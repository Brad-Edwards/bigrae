"""Principal admission for the existing provision interrupt and cleanup worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from engine.services._principal_lifecycle import PreparedRangeLifecycle

from django.db import transaction

from engine.launch_intents import request_provision_interrupt
from engine.models import InterruptState, ProvisionerLaunchIntent, Range, RangeCancellationAuthority
from shared.audit import AuditActorType, AuditEntityType, AuditEvent, audit_log, principal_actor_fields

from ._launch_authority import assert_admitted_launch_binding, assert_launch_authority_binding
from ._receipt import _revoke_receipt_verifier_for_range


def commit_range_cancellation(prepared: PreparedRangeLifecycle) -> bool:
    """Accept cleanup of one reserved generation without replacing its caller."""
    fields = dict(prepared.authority)
    with transaction.atomic():
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.native_id)
        assert_admitted_launch_binding(fields, row)
        if row.request is None or row.request.request_id != prepared.request_id or row.status != prepared.status:
            raise ValueError("Range operation changed")
        intent = _cancellable_intent(row, prepared.request_id)
        if intent is None:
            return False
        assert_launch_authority_binding(intent, row)
        if row.status == "destroying":
            return (
                intent.interrupt_state != InterruptState.NONE
                and RangeCancellationAuthority.objects.filter(provision_intent=intent).exists()
            )
        return _publish_cancellation(row, intent, prepared, fields)


def _cancellable_intent(row: Range, request_id: UUID) -> ProvisionerLaunchIntent | None:
    """Resolve only the current cancellable RAES provision generation under its native row lock."""
    if row.status not in {"pending", "provisioning", "destroying"} or row.provisioner_operation_id is None:
        return None
    intent = (
        ProvisionerLaunchIntent.objects.select_for_update()
        .filter(
            operation_id=row.provisioner_operation_id,
            authority_kind="principal",
            payload__resource="raes-range",
            payload__operation="provision",
            payload__request_id=str(request_id),
        )
        .first()
    )
    if intent is None:
        return None
    return intent


def _publish_cancellation(
    row: Range, intent: ProvisionerLaunchIntent, prepared: PreparedRangeLifecycle, fields: dict[str, object]
) -> bool:
    """Record interruption, retained initiator evidence and cancellation audit in the caller transaction."""
    if not request_provision_interrupt(row):
        return False
    RangeCancellationAuthority.objects.create(
        provision_intent=intent,
        **{key: value for key, value in fields.items() if key != "cleanup_origin_operation_uuid"},
    )
    row.status = "destroying"
    row.save(update_fields=["status", "updated_at"])
    _revoke_receipt_verifier_for_range(row)
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.RANGE,
            entity_id=row.pk,
            entity_ref=str(row.cms_range_uuid),
            action="cancel",
            **(
                {"actor_type": AuditActorType.SYSTEM, "actor_id": None}
                if fields.get("authority_kind") == "cleanup"
                else principal_actor_fields(prepared.credential.principal)
            ),
            request_id=str(prepared.request_id),
            new_state={"status": row.status, "provision_operation": str(intent.operation_id)},
        ),
        strict=True,
    )
    return True
