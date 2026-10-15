"""Live personal-token eligibility through the sole application policy provider."""

from shared.authorization import AuthorizationRequest, CredentialCeiling, TargetRef, configured_authorization_provider
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.principal_port import resolve_principal


def personal_credential_for_token(token, *, provider_check: bool = True):
    """Reconstruct admitted personal limits from the one native metadata owner."""
    from django.utils import timezone

    from shared.api_tokens.scopes import credential_ceiling
    from shared.credentials import CredentialContext, CredentialRevoked
    from shared.principal_port import principal_for_user

    if (
        not token.is_active
        or not token.has_eligible_owner
        or token.knox_token.expiry is None
        or token.knox_token.expiry <= timezone.now()
    ):
        raise CredentialRevoked("Credential unavailable")
    principal = principal_for_user(token.created_by)
    if token.principal_uuid != principal.uuid:
        raise ValueError("Credential unavailable")
    if provider_check:
        require_personal_token_grant(principal)
    return CredentialContext(
        principal,
        "personal",
        token.credential_uuid,
        credential_ceiling(token.scopes, target=token.target),
        frozenset(token.scopes),
    )


def resolve_personal_credential_reference(credential_uuid, *, provider_check: bool = True):
    """Reference lookup never accepts or retains the Knox bearer secret."""
    from shared.api_tokens.models import ApiToken
    from shared.credentials import CredentialRevoked

    token = (
        ApiToken.objects.select_related("created_by__profile", "knox_token")
        .filter(credential_uuid=credential_uuid)
        .first()
    )
    if token is None:
        raise CredentialRevoked("Credential unavailable")
    return personal_credential_for_token(token, provider_check=provider_check)


# Authorization action identifier, not a credential.
PERSONAL_TOKEN_GRANT = "installation" + ".use_personal_tokens"


def require_personal_token_grant(principal: PrincipalRef) -> None:
    """A token cannot outlive its owner's explicit current token-use grant."""
    try:
        resolve_principal(principal)
        if principal.kind != "human":
            raise ValueError("Personal credentials require a human")
        request = AuthorizationRequest(
            principal,
            PERSONAL_TOKEN_GRANT,
            TargetRef("installation"),
            ResourceScope("installation"),
            CredentialCeiling(frozenset({PERSONAL_TOKEN_GRANT})),
        )
        decision = configured_authorization_provider().check(request)
        if not decision.allowed:
            raise ValueError("Personal credential use denied")
    except Exception:
        raise ValueError("Personal credential use denied") from None
