"""Compose owning credential lifecycles for trusted durable references."""

from uuid import UUID

from django.conf import settings

from config.session_credentials import resolve_session_credential
from management.services import resolve_service_credential_reference
from shared.api_tokens.policy import resolve_personal_credential_reference
from shared.credentials import CredentialContext


def resolve_credential_reference(kind: str, credential_uuid: UUID) -> CredentialContext:
    """A UUID locates existing lifecycle state; it never proves authentication."""
    if kind in {"session", "temporary"}:
        return resolve_session_credential(credential_uuid)
    if kind == "personal":
        return resolve_personal_credential_reference(credential_uuid)
    if kind == "service":
        return resolve_service_credential_reference(credential_uuid, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE)
    raise ValueError("Credential unavailable")


def resolve_local_credential_reference(kind: str, credential_uuid: UUID) -> CredentialContext:
    """SQL lifecycle fence for a prior full admission; never provider admission."""
    if kind in {"session", "temporary"}:
        return resolve_session_credential(credential_uuid, provider_check=False)
    if kind == "personal":
        return resolve_personal_credential_reference(credential_uuid, provider_check=False)
    return resolve_credential_reference(kind, credential_uuid)
