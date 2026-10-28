"""Native range scope moves independently authorize both current parent bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope
from shared.principal_port import resolve_principal_uuid

from ._range_authority import RangeAuthorityProjection, project_range_authority

if TYPE_CHECKING:
    from engine.models import Range


@dataclass(frozen=True, slots=True)
class PreparedRangeScopeMove:
    """Frozen source runtime custody and admitted destination customer scope."""

    source: RangeAuthorityProjection
    target: ResourceScope
    status: str
    credential: CredentialContext


def prepare_range_scope_move(
    credential: CredentialContext, request_id: UUID, target: ResourceScope
) -> PreparedRangeScopeMove:
    """Admit both parents and destination ownership before moving runtime custody."""
    from engine.models import Range
    from workspaces.services import resolve_range_scope_binding

    source = project_range_authority(request_id)
    current = resolve_current_credential(credential)
    for scope in (source.scope, target):
        resolve_range_scope_binding(scope)
        parent = scope_parent(scope)
        require_authorization(
            AuthorizationRequest(current.principal, f"{parent.type}.manage_range_scope", parent, scope, current.ceiling)
        )
    if resolve_current_credential(current) != current or project_range_authority(request_id) != source:
        raise ValueError("Range scope changed")
    row = Range.objects.get(pk=source.native_id)
    _assert_move_supported(row)
    if source.scope != target:
        resolve_range_scope_binding(target, member=resolve_principal_uuid(source.owner_principal_uuid))
    return PreparedRangeScopeMove(source, target, row.status, current)


def _assert_move_supported(row: Range, *, allow_event: bool = False, allow_principal_vpn: bool = False) -> None:
    """Reject scope changes for runtime states that cannot safely move."""
    if row.status not in {"ready", "paused", "failed"} or (row.event_uuid is not None and not allow_event):
        raise ValueError("Range scope move unavailable")
    if not allow_principal_vpn and (
        row.vpn_access_binding is not None or row.vpn_profiles.filter(secret_removed_at__isnull=True).exists()
    ):
        raise ValueError("Range VPN custody must be retired first")


def commit_range_scope_move(prepared: PreparedRangeScopeMove) -> bool:
    """Fence original custody and commit the independently admitted scope move."""
    from engine.models import Range
    from workspaces.services import resolve_range_scope_binding

    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority_history import record_range_authority_change

    if not isinstance(prepared, PreparedRangeScopeMove):
        raise ValueError("Range scope unavailable")
    with transaction.atomic():
        scopes = sorted(
            {prepared.source.scope, prepared.target},
            key=lambda scope: tuple(
                str(value or "")
                for value in (scope.kind, scope.account_uuid, scope.organization_uuid, scope.workspace_uuid)
            ),
        )
        locked = {scope: resolve_range_scope_binding(scope, for_update=True) for scope in scopes}
        if prepared.source.scope != prepared.target:
            resolve_range_scope_binding(
                prepared.target, member=resolve_principal_uuid(prepared.source.owner_principal_uuid), for_update=True
            )
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.source.native_id)
        if resolve_local_credential(prepared.credential) != prepared.credential:
            raise ValueError("Range scope authority changed")
        if project_range_authority(prepared.source.request_uuid) != prepared.source or row.status != prepared.status:
            raise ValueError("Range scope changed")
        _assert_move_supported(row)
        destination = locked[prepared.target]
        if prepared.target == prepared.source.scope:
            return False
        if row.owner_principal_uuid is None:
            raise ValueError("Range owner unavailable")
        resolve_range_scope_binding(
            prepared.target, member=resolve_principal_uuid(row.owner_principal_uuid), for_update=True
        )
        from ._receipt import _revoke_receipt_verifier_for_range

        _revoke_receipt_verifier_for_range(row)
        previous_digest = range_runtime_binding_digest(row)
        row.scope_kind = destination.kind
        row.account_id = destination.account_id
        row.organization_id = destination.organization_id
        row.workspace_id = destination.workspace_id
        row.save(update_fields=["scope_kind", "account_id", "organization_id", "workspace_id", "updated_at"])
        record_range_authority_change(row, prepared.credential, "scope", previous_digest)
        from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields

        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.cms_range_uuid),
                action=AuditAction.UPDATE,
                **principal_actor_fields(prepared.credential.principal),
                request_id=str(prepared.source.request_uuid),
                previous_state={"workspace_uuid": str(prepared.source.scope.workspace_uuid)},
                new_state={"workspace_uuid": str(prepared.target.workspace_uuid)},
                context="range workspace scope reassignment",
            ),
            strict=True,
        )
        return True
