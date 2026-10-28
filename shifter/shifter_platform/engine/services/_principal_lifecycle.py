"""Stage native pause/resume policy before the owning CMS/Engine transaction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import ProvisionerLaunchIntent, Range, RangeAuthorityChange
from uuid import UUID

from django.db import transaction

from shared.audit import AuditActorType, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import TargetRef
from shared.credentials import CredentialContext
from shared.raes.runtime_target import is_raes_provisioning_plan

_RANGE_OPERATION_UNAVAILABLE = "Range operation unavailable"


@dataclass(frozen=True, slots=True)
class PreparedRangeLifecycle:
    """In-process owning-service facts; never an HTTP or worker credential."""

    request_id: UUID
    operation: str
    native_id: int
    status: str
    authority: tuple[tuple[str, object], ...]
    credential: CredentialContext


def prepare_range_lifecycle(
    credential: CredentialContext,
    request_id: UUID,
    operation: str,
    *,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
) -> PreparedRangeLifecycle:
    """Resolve the actual range and common action without holding row locks."""
    from engine.ecs._config import _get_engine_task_config
    from engine.models import Range

    from ._launch_authority import admit_launch_authority

    if operation not in {"pause", "resume", "destroy", "cancel"} or _get_engine_task_config() is None:
        raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
    row = Range.objects.filter(request__request_id=request_id).first()
    if row is None or (operation in {"pause", "resume"} and is_raes_provisioning_plan(row.range_config)):
        raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
    resource = "raes-range" if is_raes_provisioning_plan(row.range_config) else "range"
    if operation == "destroy" and row.status in {"pending", "provisioning"}:
        if resource != "raes-range":
            raise ValueError("Provision generation cannot be safely interrupted")
        operation = "cancel"
    payload = {"version": 1, "resource": resource, "operation": operation, "request_id": str(request_id)}
    authority = admit_launch_authority(
        payload,
        credential,
        authority_action=authority_action,
        authority_target=authority_target,
    )
    return PreparedRangeLifecycle(request_id, operation, row.pk, row.status, tuple(authority.items()), credential)


def commit_prepared_range_lifecycle(prepared: PreparedRangeLifecycle) -> bool:
    """Fence facts and commit status, immutable input, queue and audit together."""

    if not isinstance(prepared, PreparedRangeLifecycle) or prepared.operation not in {
        "pause",
        "resume",
        "destroy",
        "cancel",
    }:
        raise ValueError(_RANGE_OPERATION_UNAVAILABLE)
    if prepared.operation == "cancel":
        from ._principal_cancellation import commit_range_cancellation

        return commit_range_cancellation(prepared)
    if prepared.operation == "destroy":
        return _commit_destroy(prepared)
    return _commit_pause_resume(prepared)


def _commit_destroy(prepared: PreparedRangeLifecycle, *, _verification_retry: bool = False) -> bool:
    """Commit only the admitted native destruction generation and its durable queue."""
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Range

    from ._launch_authority import assert_admitted_launch_binding
    from ._receipt import _revoke_receipt_verifier_for_range

    fields = dict(prepared.authority)
    with transaction.atomic():
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.native_id)
        assert_admitted_launch_binding(fields, row)
        _require_prepared_lifecycle_row(row, prepared)
        if row.status == "destroyed" and not _reset_unverified_destroy(row, fields, _verification_retry):
            return False
        if row.status in {"pending", "provisioning"} and not (
            _is_undispatched_cleanup(row, fields) or _is_quiescent_warm_claim_cleanup(row, fields)
        ):
            raise ValueError("Provision generation requires native interruption")
        if row.status == "destroying" and _destroy_already_queued(row, fields):
            return True
        resource = "raes-range" if is_raes_provisioning_plan(row.range_config) else "range"
        row.status = "destroying"
        row.save(update_fields=["status", "updated_at"])
        _revoke_receipt_verifier_for_range(row)
        payload = {"version": 1, "resource": resource, "operation": "destroy", "request_id": str(prepared.request_id)}
        identifier = _enqueue_admitted_provisioner_launch(
            [resource, "destroy", "--request-id", str(prepared.request_id)], payload, fields
        )
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.cms_range_uuid),
                action="deprovision",
                **(
                    {"actor_type": AuditActorType.SYSTEM, "actor_id": None}
                    if fields.get("authority_kind") == "cleanup"
                    else principal_actor_fields(prepared.credential.principal)
                ),
                request_id=str(prepared.request_id),
                new_state={
                    "status": "destroying",
                    "launch_intent": identifier,
                    "initiating_principal_uuid": str(prepared.credential.principal.uuid),
                },
            ),
            strict=True,
        )
        return True


def _require_prepared_lifecycle_row(row: Range, prepared: PreparedRangeLifecycle) -> None:
    """Require the exact admitted request and state under the runtime row lock."""
    if row.request is None or row.request.request_id != prepared.request_id or row.status != prepared.status:
        raise ValueError("Range operation changed")


def _reset_unverified_destroy(row: Range, fields: Mapping[str, object], verification_retry: bool) -> bool:
    """Reserve fresh cleanup only after its owner proves terminal but unverified absence."""
    if not verification_retry or fields.get("authority_kind") != "cleanup":
        return False
    # The retry owner retains target/intent locks and exact resource custody.
    # A fresh operation preserves the immutable prior input and observation.
    row.provisioner_operation = ""
    row.provisioner_operation_id = None
    row.save(update_fields=["provisioner_operation", "provisioner_operation_id"])
    return True


def prepare_system_range_cleanup(request_id: UUID) -> PreparedRangeLifecycle:
    """Derive bounded teardown from an existing admitted generation, never an absent actor."""
    from engine.ecs._config import _get_engine_task_config
    from engine.models import Range
    from shared.operation_authority import credential_reference_from_record

    from ._launch_cleanup_authority import prepare_cleanup_authority
    from ._range_authority_history import current_range_cleanup_origin

    row = Range.objects.select_related("request").get(request__request_id=request_id)
    if _get_engine_task_config() is None or row.owner_principal_uuid is None:
        raise ValueError("Cleanup authority unavailable")
    origin = current_range_cleanup_origin(row)
    if origin is None:
        raise ValueError("Cleanup authority unavailable")
    resource = "raes-range" if is_raes_provisioning_plan(row.range_config) else "range"
    payload = {"version": 1, "resource": resource, "operation": "destroy", "request_id": str(request_id)}
    fields = prepare_cleanup_authority(payload, origin.operation_id)
    operation = "destroy"
    if _cleanup_needs_cancellation(row, fields, origin):
        if resource != "raes-range":
            raise ValueError("Provision generation cannot be safely interrupted")
        operation = "cancel"
    return PreparedRangeLifecycle(
        request_id, operation, row.pk, row.status, tuple(fields.items()), credential_reference_from_record(origin)
    )


def _cleanup_needs_cancellation(
    row: Range, fields: Mapping[str, object], origin: ProvisionerLaunchIntent | RangeAuthorityChange
) -> bool:
    """Distinguish a live provisioner from a safely quiescent retained generation."""
    from engine.models import InterruptState

    if row.status in {"pending", "provisioning"}:
        return not (_is_undispatched_cleanup(row, fields) or _is_quiescent_warm_claim_cleanup(row, fields))
    return (
        row.status == "destroying"
        and row.provisioner_operation_id == origin.operation_id
        and origin.payload.get("operation") == "provision"
        and getattr(origin, "interrupt_state", InterruptState.NONE) != InterruptState.NONE
    )


def _cleanup_origin_for_fields(
    fields: Mapping[str, object],
) -> ProvisionerLaunchIntent | RangeAuthorityChange | None:
    """Resolve only a native UUID retained in server-created cleanup fields."""
    from ._range_authority_history import cleanup_origin_record

    identifier = fields.get("cleanup_origin_operation_uuid")
    return cleanup_origin_record(identifier if isinstance(identifier, UUID) else None)


def _is_quiescent_warm_claim_cleanup(row: Range, fields: Mapping[str, object]) -> bool:
    """A claim crash before activation has only a completed quarantine producer."""
    from engine.models import (
        OperationResultDisposition,
        OperationResultInbox,
        ProvisionerLaunchIntent,
        RangeAuthorityChange,
        WarmRangeGeneration,
    )
    from shared.operation_results import ResultStep

    origin = _cleanup_origin_for_fields(fields)
    if (
        fields.get("authority_kind") != "cleanup"
        or row.request is None
        or row.provisioner_operation_id is None
        or not isinstance(origin, RangeAuthorityChange)
        or origin.operation != "scope"
        or origin.range_id != row.pk
        or origin.resource_generation != row.resource_generation
        or not WarmRangeGeneration.objects.filter(
            range=row,
            request_id=row.request.request_id,
            state="retiring",
            claimed_by_request_id__isnull=False,
            ready_at__isnull=False,
            operation_id=row.provisioner_operation_id,
        ).exists()
    ):
        return False
    previous = ProvisionerLaunchIntent.objects.filter(
        operation_id=row.provisioner_operation_id,
        authority_kind="warm_pool",
        payload__operation="provision",
        payload__request_id=str(row.request.request_id),
        authority_binding_digest=origin.previous_binding_digest,
    ).first()
    return (
        previous is not None
        and OperationResultInbox.objects.filter(
            operation_id=previous.operation_id,
            request_id=row.request.request_id,
            result_step=ResultStep.RAES_TERMINAL_READY,
            disposition=OperationResultDisposition.APPLIED,
        ).exists()
    )


def _is_undispatched_cleanup(row: Range, fields: Mapping[str, object]) -> bool:
    """Only a retained creation receipt can clean a generation never queued."""
    from engine.models import RangeAuthorityChange

    origin = _cleanup_origin_for_fields(fields)
    return bool(
        fields.get("authority_kind") == "cleanup"
        and row.provisioner_operation_id is None
        and isinstance(origin, RangeAuthorityChange)
        and origin.operation == "create"
        and origin.range_id == row.pk
        and origin.resource_generation == row.resource_generation
    )


def _commit_pause_resume(prepared: PreparedRangeLifecycle) -> bool:
    """Atomically commit a previously admitted pause or resume against unchanged runtime custody."""
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Range

    from ._launch_authority import assert_admitted_launch_binding

    required, target, settled = (
        ("ready", "pausing", "paused") if prepared.operation == "pause" else ("paused", "resuming", "ready")
    )
    fields = dict(prepared.authority)
    command = ["range", prepared.operation, "--request-id", str(prepared.request_id)]
    payload = {
        "version": 1,
        "resource": "range",
        "operation": prepared.operation,
        "request_id": str(prepared.request_id),
    }
    with transaction.atomic():
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.native_id)
        assert_admitted_launch_binding(fields, row)
        _require_prepared_lifecycle_row(row, prepared)
        if row.status in {target, settled}:
            return True
        if row.status != required:
            return False
        row.status = target
        row.save(update_fields=["status", "updated_at"])
        identifier = _enqueue_admitted_provisioner_launch(command, payload, fields)
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RANGE,
                entity_id=row.pk,
                entity_ref=str(row.cms_range_uuid),
                action=prepared.operation,
                **principal_actor_fields(prepared.credential.principal),
                request_id=str(prepared.request_id),
                new_state={"status": target, "launch_intent": identifier},
            ),
            strict=True,
        )
        return True


def _destroy_already_queued(row: Range, fields: dict[str, object]) -> bool:
    """Retain a queued teardown generation while allowing recorded cleanup to replace ordinary dispatch."""
    from engine.models import ProvisionerLaunchIntent

    existing = (
        ProvisionerLaunchIntent.objects.filter(operation_id=row.provisioner_operation_id).first()
        if row.provisioner_operation_id is not None
        else None
    )
    return fields.get("authority_kind") != "cleanup" or (existing is not None and existing.authority_kind == "cleanup")
