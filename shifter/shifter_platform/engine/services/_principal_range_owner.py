"""Principal-aware business ownership changes preserve historical attribution."""

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef
from shared.principal_port import resolve_principal

from ._range_authority import RangeAuthorityProjection, project_range_authority


@dataclass(frozen=True, slots=True)
class PreparedRangeOwnerMove:
    """Frozen source custody, destination owner and original reassignment proof."""

    source: RangeAuthorityProjection
    new_owner: PrincipalRef
    status: str
    authority: tuple[tuple[str, object], ...]
    credential: CredentialContext
    event_uuid: UUID | None = None


def prepare_range_owner_move(
    credential: CredentialContext, request_id: UUID, new_owner: PrincipalRef
) -> PreparedRangeOwnerMove:
    """Admit reassignment and verify the destination owner current scope membership."""
    from engine.models import Range
    from workspaces.services import resolve_range_scope_binding

    from ._launch_authority import admit_launch_authority
    from ._principal_range_scope import _assert_move_supported

    owner = resolve_principal(new_owner)
    source = project_range_authority(request_id)
    fields = admit_launch_authority(
        {"version": 1, "resource": "range", "operation": "reassign", "request_id": str(request_id)}, credential
    )
    row = Range.objects.get(pk=source.native_id)
    _assert_move_supported(row)
    from ._range_by_request import principal_range_owner_reassignment_available

    if not principal_range_owner_reassignment_available(row):
        raise ValueError("Range owner unavailable")
    if owner.uuid != source.owner_principal_uuid:
        resolve_range_scope_binding(source.scope, member=owner)
    return PreparedRangeOwnerMove(source, owner, row.status, tuple(fields.items()), credential)


def prepare_event_range_owner_move(
    credential: CredentialContext, request_id: UUID, new_owner: PrincipalRef, event_uuid: UUID
) -> PreparedRangeOwnerMove:
    """Admit an event-bound spare handoff without inventing SQL membership."""
    from engine.models import Range
    from shared.authorization import AuthorizationRequest, TargetRef
    from shared.authorization.policy import require_authorization
    from shared.credentials import resolve_current_credential

    from ._launch_authority import admitted_range_authority_fields
    from ._principal_range_scope import _assert_move_supported

    current = resolve_current_credential(credential)
    owner = resolve_principal(new_owner)
    source = project_range_authority(request_id)
    target = TargetRef("event", event_uuid)
    if source.parent != target:
        raise ValueError("Event range unavailable")
    require_authorization(
        AuthorizationRequest(current.principal, "event.manage_ranges", target, source.scope, current.ceiling)
    )
    row = Range.objects.get(pk=source.native_id)
    _assert_move_supported(row, allow_event=True, allow_principal_vpn=True)
    fields = admitted_range_authority_fields(current, source, row, action="event.manage_ranges", target=target)
    return PreparedRangeOwnerMove(source, owner, row.status, tuple(fields.items()), current, event_uuid)


def commit_range_owner_move(prepared: PreparedRangeOwnerMove) -> bool:
    """Commit reassignment only while admitted runtime custody remains unchanged."""
    from engine.models import Range, Request
    from workspaces.services import resolve_range_scope_binding

    from ._launch_authority import assert_admitted_launch_binding, range_runtime_binding_digest
    from ._principal_range_scope import _assert_move_supported
    from ._range_authority_history import record_range_authority_change
    from ._receipt import _revoke_receipt_verifier_for_range

    if not isinstance(prepared, PreparedRangeOwnerMove):
        raise ValueError("Range owner unavailable")
    with transaction.atomic():
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.source.native_id)
        if row.request_id is None:
            raise ValueError("Range request unavailable")
        request = Request.objects.select_for_update().get(pk=row.request_id)
        assert_admitted_launch_binding(dict(prepared.authority), row)
        if project_range_authority(prepared.source.request_uuid) != prepared.source or row.status != prepared.status:
            raise ValueError("Range owner changed")
        _assert_move_supported(
            row,
            allow_event=prepared.event_uuid is not None,
            allow_principal_vpn=prepared.event_uuid is not None,
        )
        from ._range_by_request import principal_range_owner_reassignment_available

        if not principal_range_owner_reassignment_available(row):
            raise ValueError("Range owner changed")
        owner = resolve_principal(prepared.new_owner)
        if owner.uuid == row.owner_principal_uuid:
            return False
        if prepared.event_uuid is None:
            resolve_range_scope_binding(prepared.source.scope, member=owner, for_update=True)
        elif prepared.source.parent != TargetRef("event", prepared.event_uuid):
            raise ValueError("Event range changed")
        _revoke_receipt_verifier_for_range(row)
        previous_digest = range_runtime_binding_digest(row)
        row.owner_principal_uuid = request.owner_principal_uuid = owner.uuid
        update_fields = ["owner_principal_uuid", "updated_at"]
        if row.vpn_access_binding is not None:
            row.vpn_access_binding = {
                **row.vpn_access_binding,
                "owner_principal_uuid": str(owner.uuid),
            }
            update_fields.append("vpn_access_binding")
        row.save(update_fields=update_fields)
        request.save(update_fields=["owner_principal_uuid"])
        record_range_authority_change(row, prepared.credential, "owner", previous_digest)
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.cms_range_uuid),
                action=AuditAction.UPDATE,
                **principal_actor_fields(prepared.credential.principal),
                request_id=str(prepared.source.request_uuid),
                previous_state={"owner_principal_uuid": str(prepared.source.owner_principal_uuid)},
                new_state={"owner_principal_uuid": str(owner.uuid)},
                context="range ownership reassignment",
            ),
            strict=True,
        )
        return True
