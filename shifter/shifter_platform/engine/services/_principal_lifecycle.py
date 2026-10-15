"""Stage native pause/resume policy before the owning CMS/Engine transaction."""

from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from shared.audit import AuditActorType, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext
from shared.raes.runtime_target import is_raes_provisioning_plan


@dataclass(frozen=True, slots=True)
class PreparedRangeLifecycle:
    """In-process owning-service facts; never an HTTP or worker credential."""

    request_id: UUID
    operation: str
    native_id: int
    status: str
    authority: tuple[tuple[str, object], ...]
    credential: CredentialContext


def prepare_range_lifecycle(credential: CredentialContext, request_id: UUID, operation: str) -> PreparedRangeLifecycle:
    """Resolve the actual range and common action without holding row locks."""
    from engine.ecs._config import _get_engine_task_config
    from engine.models import Range

    from ._launch_authority import admit_launch_authority

    if operation not in {"pause", "resume", "destroy", "cancel"} or _get_engine_task_config() is None:
        raise ValueError("Range operation unavailable")
    row = Range.objects.filter(request__request_id=request_id).first()
    if row is None or (operation in {"pause", "resume"} and is_raes_provisioning_plan(row.range_config)):
        raise ValueError("Range operation unavailable")
    resource = "raes-range" if is_raes_provisioning_plan(row.range_config) else "range"
    if operation == "destroy" and row.status in {"pending", "provisioning"}:
        if resource != "raes-range":
            raise ValueError("Provision generation cannot be safely interrupted")
        operation = "cancel"
    payload = {"version": 1, "resource": resource, "operation": operation, "request_id": str(request_id)}
    authority = admit_launch_authority(payload, credential)
    return PreparedRangeLifecycle(request_id, operation, row.pk, row.status, tuple(authority.items()), credential)


def commit_prepared_range_lifecycle(prepared: PreparedRangeLifecycle) -> bool:
    """Fence facts and commit status, immutable input, queue and audit together."""
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Range

    from ._launch_authority import assert_admitted_launch_binding

    if not isinstance(prepared, PreparedRangeLifecycle) or prepared.operation not in {
        "pause",
        "resume",
        "destroy",
        "cancel",
    }:
        raise ValueError("Range operation unavailable")
    if prepared.operation == "cancel":
        from ._principal_cancellation import commit_range_cancellation

        return commit_range_cancellation(prepared)
    if prepared.operation == "destroy":
        return _commit_destroy(prepared)
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
        if row.request.request_id != prepared.request_id or row.status != prepared.status:
            raise ValueError("Range operation changed")
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


def _commit_destroy(prepared: PreparedRangeLifecycle) -> bool:
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import ProvisionerLaunchIntent, Range

    from ._launch_authority import assert_admitted_launch_binding
    from ._receipt import _revoke_receipt_verifier_for_range

    fields = dict(prepared.authority)
    with transaction.atomic():
        row = Range.objects.select_for_update(of=("self",)).select_related("request").get(pk=prepared.native_id)
        assert_admitted_launch_binding(fields, row)
        if row.request.request_id != prepared.request_id or row.status != prepared.status:
            raise ValueError("Range operation changed")
        if row.status == "destroyed":
            return False
        if row.status in {"pending", "provisioning"}:
            raise ValueError("Provision generation requires native interruption")
        if row.status == "destroying":
            existing = ProvisionerLaunchIntent.objects.filter(operation_id=row.provisioner_operation_id).first()
            if fields.get("authority_kind") != "cleanup" or (
                existing is not None and existing.authority_kind == "cleanup"
            ):
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


def prepare_system_range_cleanup(request_id: UUID) -> PreparedRangeLifecycle:
    """Derive bounded teardown from an existing admitted generation, never an absent actor."""
    from engine.ecs._config import _get_engine_task_config
    from engine.models import InterruptState, Range
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
    if row.status in {"pending", "provisioning"} or (
        row.status == "destroying"
        and origin.payload.get("operation") == "provision"
        and getattr(origin, "interrupt_state", InterruptState.NONE) != InterruptState.NONE
    ):
        if resource != "raes-range":
            raise ValueError("Provision generation cannot be safely interrupted")
        operation = "cancel"
    return PreparedRangeLifecycle(
        request_id, operation, row.pk, row.status, tuple(fields.items()), credential_reference_from_record(origin)
    )
