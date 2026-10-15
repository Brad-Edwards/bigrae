"""Tenant application authority, separate from isolated worker/IAM admission."""

from uuid import UUID

from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.policy import require_authorization
from shared.credentials import CredentialContext, resolve_credential_scope, resolve_current_credential
from shared.exceptions import ValidationError
from shared.operation_authority import credential_reference_fields, credential_reference_from_record


def admit_plugin_authority(credential: CredentialContext, organization_uuid: UUID) -> dict[str, object]:
    """Resolve the actual organization/account before any secret or installation I/O."""
    try:
        current = resolve_current_credential(credential)
        target = TargetRef("organization", organization_uuid)
        scope = resolve_credential_scope(target)
        require_authorization(
            AuthorizationRequest(current.principal, "organization.manage_adapters", target, scope, current.ceiling)
        )
        return {
            **credential_reference_fields(current),
            "authority_kind": "principal",
            "authority_action": "organization.manage_adapters",
            "authority_target_type": target.type,
            "authority_target_uuid": target.uuid,
            "authority_scope_kind": scope.kind,
            "authority_account_uuid": scope.account_uuid,
            "authority_organization_uuid": scope.organization_uuid,
            "authority_workspace_uuid": scope.workspace_uuid,
            "authority_owner_uuid": current.principal.uuid,
        }
    except Exception:
        raise ValidationError("Plugin authority unavailable") from None


def validate_plugin_scope(row, fields: dict[str, object]) -> None:
    """An archived, rebound or conflicting organization cannot rehome a plugin."""
    names = (
        "authority_scope_kind",
        "authority_account_uuid",
        "authority_organization_uuid",
        "authority_workspace_uuid",
    )
    if row.authority_kind != "principal" or any(getattr(row, name) != fields[name] for name in names):
        raise ValidationError("Plugin authority unavailable")


def reauthorize_plugin_initiator(row) -> None:
    """The controller and retries intersect the original exact credential ceiling."""
    fields = admit_plugin_authority(credential_reference_from_record(row), row.organization_uuid)
    validate_plugin_scope(row, fields)
    if (
        fields["authority_target_uuid"] != row.authority_target_uuid
        or row.authority_action != "organization.manage_adapters"
    ):
        raise ValidationError("Plugin authority unavailable")
