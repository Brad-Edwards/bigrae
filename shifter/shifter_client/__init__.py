"""Supported native-cloud Shifter service transport; no application internals."""

import datetime
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

import google.auth
import requests
from google.auth import impersonated_credentials
from google.auth.compute_engine import credentials as compute_credentials
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2 import service_account

_CLOUD_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
_AWS_TOKEN_SECONDS = 900
_AWS_RENEW_BEFORE = datetime.timedelta(seconds=60)


def _utcnow() -> datetime.datetime:
    """Return the current UTC time; tests replace this seam."""
    return datetime.datetime.now(datetime.UTC)


def _validated_audience(audience: str) -> str:
    """Require an absolute HTTPS application audience without credentials or query."""
    parsed = urlsplit(audience)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Application audience must be an absolute HTTPS URL")
    return audience


class _AwsWebIdentitySession(requests.Session):
    """Attach an AWS STS web identity token and renew it before expiry.

    The STS client uses the botocore credential chain, so the source role
    credentials renew natively as well. The token is never part of a repr.
    """

    def __init__(self, sts: object, audience: str) -> None:
        super().__init__()
        self._sts = sts
        self._audience = audience
        self._lock = threading.Lock()
        self._token = ""
        self._expiration = datetime.datetime.min.replace(tzinfo=datetime.UTC)

    def _current(self) -> str:
        with self._lock:
            if not self._token or _utcnow() >= self._expiration - _AWS_RENEW_BEFORE:
                response = self._sts.get_web_identity_token(
                    Audience=[self._audience],
                    SigningAlgorithm="ES384",
                    DurationSeconds=_AWS_TOKEN_SECONDS,
                )
                token = response.get("WebIdentityToken")
                expiration = response.get("Expiration")
                if (
                    not isinstance(token, str)
                    or not token
                    or not token.isascii()
                    or any(character.isspace() for character in token)
                    or not isinstance(expiration, datetime.datetime)
                    or expiration.tzinfo is None
                    or expiration <= _utcnow()
                ):
                    raise ValueError("AWS web identity token unavailable")
                self._token, self._expiration = token, expiration
            return self._token

    def request(self, method, url, **kwargs):  # type: ignore[no-untyped-def,override]
        """Send one request with current proof; never replay it after a 401."""
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self._current()}"
        return super().request(method, url, headers=headers, **kwargs)

    def __repr__(self) -> str:
        return "<AwsWebIdentitySession>"


@dataclass
class ServiceClient:
    """Separate automatically refreshed app and cloud transports from native credentials."""

    audience: str
    application: requests.Session
    cloud: object

    @classmethod
    def from_aws(
        cls, audience: str, *, region: str | None = None, session: object | None = None
    ) -> "ServiceClient":
        """Use the botocore credential chain and STS outbound web identity.

        The application transport sends a renewed STS web identity token for
        exactly this audience. The cloud transport is the boto3 session, whose
        credentials botocore renews natively. Static keys are not required.
        """
        _validated_audience(audience)
        if session is None:
            try:
                import boto3
            except ImportError:
                raise ValueError(
                    "AWS support requires the 'aws' extra (boto3)"
                ) from None
            session = boto3.Session(region_name=region)
        application = _AwsWebIdentitySession(
            session.client("sts", region_name=region), audience
        )
        return cls(audience, application, session)

    @classmethod
    def from_adc(
        cls, audience: str, *, target_principal: str | None = None
    ) -> "ServiceClient":
        """Use attached identity, impersonated ADC, or explicit ADC impersonation.

        Source credentials must themselves remain renewable. External federation
        uses google-auth's external-account ADC support and an explicitly named
        target service account. Service-account JSON keys are not supported.
        """
        _validated_audience(audience)
        source, _ = google.auth.default(scopes=_CLOUD_SCOPES)
        if isinstance(source, service_account.Credentials):
            raise ValueError("Static service-account keys are not supported")
        cloud = source
        if target_principal is not None:
            if (
                not target_principal.endswith(".gserviceaccount.com")
                or "@" not in target_principal
            ):
                raise ValueError("An explicit service-account email is required")
            cloud = impersonated_credentials.Credentials(
                source_credentials=source,
                target_principal=target_principal,
                target_scopes=_CLOUD_SCOPES,
                lifetime=3600,
            )
        if isinstance(cloud, impersonated_credentials.Credentials):
            application = impersonated_credentials.IDTokenCredentials(
                cloud,
                target_audience=audience,
                include_email=True,
            )
        elif isinstance(cloud, compute_credentials.Credentials):
            application = compute_credentials.IDTokenCredentials(
                Request(),
                target_audience=audience,
                use_metadata_identity_endpoint=True,
            )
        else:
            raise ValueError(
                "Non-attached ADC requires an explicit target service account"
            )
        # Native before_request refreshes expired proof. Do not replay arbitrary
        # mutations after a 401: retries require application idempotency semantics.
        return cls(
            audience,
            AuthorizedSession(application, max_refresh_attempts=0),
            AuthorizedSession(cloud, max_refresh_attempts=0),
        )

    def request(self, method: str, path: str, **kwargs):
        """Send one app request to the bound audience; never follow redirects."""
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or not path.startswith("/")
            or path.startswith("//")
        ):
            raise ValueError("Application requests require a relative absolute path")
        if "allow_redirects" in kwargs or "auth" in kwargs:
            raise ValueError(
                "Application authentication and redirects are transport-owned"
            )
        kwargs.setdefault("timeout", 30)
        return self.application.request(
            method, self.audience.rstrip("/") + path, allow_redirects=False, **kwargs
        )

    def close(self) -> None:
        """Release the native HTTP connection pools."""
        self.application.close()
        close = getattr(self.cloud, "close", None)
        if callable(close):
            close()
