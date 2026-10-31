"""Native service proof verification at the composition boundary.

GCP admits Google service ID tokens. AWS admits STS outbound web identity
tokens. ``CLOUD_PROVIDER`` selects exactly one provider; both admit only an
explicit ``ServiceCredentialAdmission`` for the exact issuer and subject.
"""

from functools import partial

from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2 import id_token

from management.services import resolve_service_credential
from shared.aws_web_identity import valid_account, valid_issuer, valid_role_arn, verify_aws_web_identity_token
from shared.credentials import CredentialContext
from shared.service_proof import ServiceProofProfile

_GOOGLE_ISSUER = "https://accounts.google.com"


def _valid_service_subject(value: object) -> bool:
    """Return whether a Google service-account numeric identity is bounded."""
    return isinstance(value, str) and value.isascii() and value.isdecimal() and 10 <= len(value) <= 32


def _valid_service_email(value: object) -> bool:
    """Return whether claims identify a Google service-account address."""
    return isinstance(value, str) and value.endswith(".gserviceaccount.com")


def _aws_selected() -> bool:
    """Return whether the deployment selects the AWS provider."""
    return str(getattr(settings, "CLOUD_PROVIDER", "")).strip().lower() == "aws"


def _aws_account() -> str:
    """Return the configured AWS account that owns the trusted issuer."""
    return str(getattr(settings, "AWS_ACCOUNT_ID", "") or "")


def service_proof_profile() -> ServiceProofProfile:
    """Return the selected provider's issuer, audience and subject grammar."""
    if _aws_selected():
        account = _aws_account()
        issuer = str(getattr(settings, "AWS_SERVICE_TOKEN_ISSUER", "") or "")
        # An invalid issuer or account yields an empty issuer, which fails closed.
        return ServiceProofProfile(
            issuer=issuer if valid_issuer(issuer) and valid_account(account) else "",
            audience=str(getattr(settings, "AWS_SERVICE_TOKEN_AUDIENCE", "") or ""),
            subject_valid=partial(valid_role_arn, account_id=account),
        )
    return ServiceProofProfile(
        issuer=_GOOGLE_ISSUER,
        audience=str(getattr(settings, "GCP_SERVICE_TOKEN_AUDIENCE", "") or ""),
        subject_valid=_valid_service_subject,
    )


def _verify_aws(raw_token: str) -> CredentialContext:
    """Require the account issuer, exact audience, a role subject and explicit admission."""
    profile = service_proof_profile()
    account = _aws_account()
    if not valid_issuer(profile.issuer) or not valid_account(account) or not profile.audience.startswith("https://"):
        raise ValueError("Service authentication unavailable")
    try:
        subject = verify_aws_web_identity_token(
            raw_token, issuer=profile.issuer, audience=profile.audience, account_id=account
        )
    except ValueError:
        raise ValueError("Service authentication denied") from None
    return resolve_service_credential(issuer=profile.issuer, subject=subject, audience=profile.audience)


def verify_service_credential(raw_token: str) -> CredentialContext:
    """Require exact native proof, configured audience and explicit SQL admission."""
    if _aws_selected():
        return _verify_aws(raw_token)
    audience = getattr(settings, "GCP_SERVICE_TOKEN_AUDIENCE", "")
    if not audience or not audience.startswith("https://"):
        raise ValueError("Service authentication unavailable")
    claims = id_token.verify_oauth2_token(raw_token, partial(Request(), timeout=10), audience=audience)
    subject = claims.get("sub")
    email = claims.get("email")
    trusted_claims = (
        claims.get("iss") == _GOOGLE_ISSUER and claims.get("aud") == audience and claims.get("email_verified") is True
    )
    if not trusted_claims or not _valid_service_subject(subject) or not _valid_service_email(email):
        raise ValueError("Service authentication denied")
    assert isinstance(subject, str)
    return resolve_service_credential(issuer=claims["iss"], subject=subject, audience=audience)
