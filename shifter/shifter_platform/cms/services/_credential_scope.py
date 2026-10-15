"""CMS-owned range ancestry projection for credential authority checks."""

from uuid import UUID

from cms.models import RangeInstance
from shared.identity_scope import ResourceScope
from workspaces.services import resource_scope_from_ids


def range_credential_scope(range_uuid: UUID) -> ResourceScope:
    """Resolve a live range and its validated account hierarchy."""
    instance = RangeInstance.all_objects.filter(uuid=range_uuid).first()
    if instance is None:
        raise ValueError("Credential scope unavailable")
    if instance.is_deleted and instance.status not in {"destroying", "failed"}:
        raise ValueError("Credential scope unavailable")
    if instance.owner_principal_uuid is not None:
        from ._unified_authorization import range_authorization_binding

        return range_authorization_binding(instance.uuid, include_terminal=instance.is_deleted, read_only_intent=True)[
            1
        ]
    return resource_scope_from_ids(
        kind=instance.scope_kind,
        account_id=instance.account_id,
        organization_id=instance.organization_id,
        workspace_id=instance.workspace_id,
    )
