"""Invocation-local native authority for tenant pack custody and cleanup."""

from dataclasses import dataclass
from uuid import UUID

from shared.credentials import CredentialContext, resolve_local_credential
from shared.identity_scope import ResourceScope
from workspaces.services import OrganizationAuthorizationError, hierarchy_target_scope, resolve_range_scope_binding

from ._content_authority import require_content_scope


@dataclass(frozen=True, slots=True)
class TenantPackAdmission:
    """Bind current native content authority to one owning organization."""

    credential: CredentialContext
    organization_uuid: UUID
    scope: ResourceScope


def admit_tenant_pack(
    credential: CredentialContext, organization_uuid: UUID, operation: str = "manage_content"
) -> TenantPackAdmission:
    """Resolve the real owning organization and evaluate policy before SQL locks."""
    try:
        if not isinstance(credential, CredentialContext):
            raise ValueError("Native content proof required")
        scope = hierarchy_target_scope("organization", organization_uuid)
        current = require_content_scope(credential, scope, operation)
        return TenantPackAdmission(current, organization_uuid, scope)
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise OrganizationAuthorizationError("Organization access denied") from None


def fence_tenant_pack(admission: TenantPackAdmission) -> None:
    """Hold account ancestry and local credential lifecycle through the custody write."""
    try:
        if resolve_local_credential(admission.credential) != admission.credential:
            raise ValueError("Content proof changed")
        resolve_range_scope_binding(admission.scope, for_update=True)
        if hierarchy_target_scope("organization", admission.organization_uuid) != admission.scope:
            raise ValueError("Content ancestry changed")
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise OrganizationAuthorizationError("Organization access denied") from None
