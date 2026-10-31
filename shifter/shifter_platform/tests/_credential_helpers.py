"""Persist native credential owners for tests below transport admission."""

from dataclasses import replace

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY

from management.models import Principal, PrincipalSession, ProviderBinding, ServiceCredentialAdmission
from management.session_backend import SessionStore
from shared.api_tokens.scopes import KNOWN_SCOPES
from shared.credentials import CredentialContext
from shared.service_proof import service_proof_profile


def persisted_credential(credential: CredentialContext) -> CredentialContext:
    """Back an explicit bounded test context with real session or admission state."""
    principal = Principal.objects.get(uuid=credential.principal.uuid)
    if credential.kind == "service":
        binding, _ = ProviderBinding.objects.get_or_create(
            principal=principal,
            issuer="https://credential.example.test",
            subject=str(credential.credential_uuid),
        )
        ServiceCredentialAdmission.objects.get_or_create(
            uuid=credential.credential_uuid,
            defaults={
                "binding": binding,
                "audience": service_proof_profile().audience,
                "scopes": sorted(KNOWN_SCOPES),
            },
        )
    elif credential.kind == "session":
        if not PrincipalSession.objects.filter(credential_uuid=credential.credential_uuid).exists():
            user = principal.user
            if user is None:
                raise ValueError("Session credentials require their owning human")
            session = SessionStore()
            session[SESSION_KEY] = str(user.pk)
            session[BACKEND_SESSION_KEY] = "config.auth.PlatformModelBackend"
            session[HASH_SESSION_KEY] = user.get_session_auth_hash()
            session.save()
            return replace(credential, credential_uuid=session.credential_uuid)
    else:
        raise ValueError("Use the native token or temporary-account test ceremony")
    return credential
