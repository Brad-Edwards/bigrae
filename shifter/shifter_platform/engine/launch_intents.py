"""Validated durable intents for privileged provisioner Job launches."""

from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Model, QuerySet
from django.utils import timezone

from engine.launch_intents_failure import (
    PROVISIONER_DISPATCH_FAILED,
    clear_provisioner_operation_after_failure,
    fail_current_provisioner_operation,
)

# The dispatch-failure lifecycle lives in its own module (Sonar S104); re-exported
# here so existing ``from engine.launch_intents import ...`` importers are unaffected.
from engine.launch_interrupt import request_provision_interrupt
from engine.launch_payload_authority import authorize_provisioner_payload as authorize_provisioner_payload
from engine.launch_target import lock_provisioner_target
from engine.models import (
    Instance,
    OperationInput,
    ProvisionerLaunchIntent,
    ProvisionerLaunchStatus,
    Range,
    Request,
)
from engine.operation_inputs import operation_input_payload
from shared.cloud import PROVISIONER_CONTAINER_NAME
from shared.cloud.kubernetes.naming import build_idempotent_job_name
from shared.credentials import CredentialContext
from shared.operation_envelope import canonical_payload_digest

if TYPE_CHECKING:
    from typing import Any
    from uuid import UUID

    from shared.credentials import CredentialContext


_OPERATION_AUTHORITY_UNAVAILABLE = "Operation authority unavailable"


# Public surface, including the dispatch-failure names re-exported from
# ``launch_intents_failure`` (Sonar S104 split) so existing importers are unaffected.
__all__ = [
    "PROVISIONER_DISPATCH_FAILED",
    "authorize_provisioner_payload",
    "clear_provisioner_operation_after_failure",
    "command_from_payload",
    "enqueue_provisioner_launch",
    "fail_current_provisioner_operation",
    "request_provision_interrupt",
    "task_ref_for_intent",
    "validate_provisioner_command",
]

_OPERATIONS = {
    "range": {"provision", "destroy", "pause", "resume"},
    # ``activate`` (#28) hands an atomically claimed warm generation to its
    # claimant. Only the ownership-neutral raes-range path carries it; the legacy
    # user_id-bearing ``range`` path is warm-ineligible (preflight #28).
    "raes-range": {"provision", "destroy", "pause", "resume", "activate"},
    "ngfw": {"provision", "deprovision", "start", "stop"},
}


def _request_payload(command: list[str]) -> dict[str, object] | None:
    """Validate and normalize a request-id command when its shape matches."""
    if len(command) != 4 or command[2] != "--request-id":
        return None
    resource, operation, _, request_id = command
    if resource not in _OPERATIONS or operation not in _OPERATIONS[resource]:
        raise ValueError("unsupported provisioner resource or operation")
    try:
        parsed_request_id = UUID(request_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("request_id must be a UUID") from exc
    return {
        "version": 1,
        "resource": resource,
        "operation": operation,
        "request_id": str(parsed_request_id),
    }


def _legacy_range_payload(command: list[str]) -> dict[str, object] | None:
    """Validate and normalize a legacy range command when its shape matches."""
    if len(command) != 6 or command[:2] not in (["range", "provision"], ["range", "destroy"]):
        return None
    if command[2] != "--range-id" or command[4] != "--user-id":
        raise ValueError("legacy range command must use canonical identifier flags")
    try:
        range_id, user_id = int(command[3]), int(command[5])
    except ValueError as exc:
        raise ValueError("legacy identifiers must be integers") from exc
    if range_id < 0 or user_id < 0:
        raise ValueError("legacy identifiers must be non-negative")
    return {
        "version": 1,
        "resource": "range",
        "operation": command[1],
        "range_id": range_id,
        "user_id": user_id,
    }


def _split_operation_id(command: list[str]) -> tuple[list[str], str | None]:
    """Split an optional trailing ``--operation-id <uuid>`` correlation pair.

    The generation fence (``operation_id``) is carried on the launched argv so the
    provisioner tags its input read and result appends with exactly the operation
    it is executing, never "latest by request" (ADR-043). It is optional so the
    engine can validate the canonical command before the id is minted, and add it
    only when reconstructing the dispatch argv from a persisted intent.
    """
    if len(command) >= 2 and command[-2] == "--operation-id":
        try:
            operation_id = str(UUID(command[-1]))
        except (TypeError, ValueError) as exc:
            raise ValueError("operation_id must be a UUID") from exc
        return command[:-2], operation_id
    return command, None


def validate_provisioner_command(command: list[str]) -> dict[str, object]:
    """Return a versioned, secret-free payload for one canonical CLI command."""
    if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
        raise ValueError("provisioner command must be a list of strings")
    base, operation_id = _split_operation_id(command)
    payload: dict[str, object] | None
    if base[:1] == ["vpn-profile"]:
        from engine.services._vpn_profile_operations import validate_vpn_command

        payload = validate_vpn_command(base)
    else:
        payload = _request_payload(base) or _legacy_range_payload(base)
    if payload is None:
        raise ValueError("command does not match a canonical provisioner launch shape")
    if operation_id is not None:
        payload["operation_id"] = operation_id
    return payload


def command_from_payload(payload: dict[str, object]) -> list[str]:
    """Reconstruct and revalidate the canonical command at the trust boundary."""
    if payload.get("version") != 1:
        raise ValueError("unsupported provisioner launch intent version")
    resource = payload.get("resource")
    operation = payload.get("operation")
    if "request_id" in payload:
        command = [str(resource), str(operation), "--request-id", str(payload["request_id"])]
        if resource == "vpn-profile":
            command.extend(["--profile-id", str(payload["profile_id"])])
    else:
        command = [
            str(resource),
            str(operation),
            "--range-id",
            str(payload.get("range_id")),
            "--user-id",
            str(payload.get("user_id")),
        ]
    if payload.get("operation_id"):
        command = [*command, "--operation-id", str(payload["operation_id"])]
    if validate_provisioner_command(command) != payload:
        raise ValueError("provisioner launch intent payload is not canonical")
    return command


def _lock_for_generation[ModelT: Model](
    queryset: QuerySet[ModelT], expected_operation_id: UUID | str | None
) -> QuerySet[ModelT]:
    """Lock a domain projection when validating a persisted operation generation."""
    return queryset.select_for_update() if expected_operation_id is not None else queryset


def _require_current_generation(row: Range | Instance, expected_operation_id: UUID | str | None) -> None:
    """Reject an intent that belongs to a superseded domain-operation generation."""
    if expected_operation_id is not None and row.provisioner_operation_id != UUID(str(expected_operation_id)):
        raise ValueError("launch intent operation generation is no longer current")
    principal_owned = (isinstance(row, Range) and row.owner_principal_uuid is not None) or (
        isinstance(row, Instance) and row.request is not None and row.request.owner_principal_uuid is not None
    )
    if principal_owned and expected_operation_id is not None:
        from engine.services._launch_authority import assert_launch_authority_binding

        intent = ProvisionerLaunchIntent.objects.filter(operation_id=expected_operation_id).first()
        if intent is None:
            raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
        assert_launch_authority_binding(intent, row)


def _authorize_legacy_range(
    payload: dict[str, object],
    target: Range | Instance | None,
    expected_operation_id: UUID | str | None,
) -> None:
    """Authorize a legacy range-id/user-id payload."""
    range_rows = _lock_for_generation(Range.objects.all(), expected_operation_id)
    row = target if isinstance(target, Range) else range_rows.filter(pk=int(str(payload.get("range_id")))).first()
    if row is None or row.user_id != payload.get("user_id"):
        raise ValueError("legacy launch intent does not match an owned range")
    _require_current_generation(row, expected_operation_id)
    allowed_states = {
        "provision": {Range.Status.PENDING, Range.Status.PROVISIONING},
        "destroy": {Range.Status.DESTROYING},
    }
    if row.status not in allowed_states[str(payload.get("operation"))]:
        raise ValueError("range state does not authorize the requested operation")


def _authorize_request_range(
    payload: dict[str, object],
    request: Request,
    target: Range | Instance | None,
    expected_operation_id: UUID | str | None,
) -> None:
    """Authorize a request-based Range or RAES Range payload."""
    range_rows = _lock_for_generation(Range.objects.all(), expected_operation_id)
    row = target if isinstance(target, Range) else range_rows.filter(request=request).first()
    if row is None:
        raise ValueError("launch intent request has no range")
    _require_current_generation(row, expected_operation_id)
    operation = str(payload.get("operation"))
    if operation == "activate":
        # Warm activation (#28) authority lives on its own seam; the claimed warm
        # generation on a quarantined range is the authority, not a READY range.
        from engine.warm_activation_authz import authorize_warm_activation

        authorize_warm_activation(request, row)
        return
    allowed_states = {
        "provision": {Range.Status.PENDING, Range.Status.PROVISIONING},
        "destroy": {Range.Status.DESTROYING},
        "pause": {Range.Status.PAUSING},
        "resume": {Range.Status.RESUMING},
    }
    if row.status not in allowed_states[operation]:
        raise ValueError("range state does not authorize the requested operation")


def _authorize_request_ngfw(
    payload: dict[str, object],
    request: Request,
    target: Range | Instance | None,
    expected_operation_id: UUID | str | None,
) -> None:
    """Authorize a request-based NGFW payload."""
    ngfw_rows = _lock_for_generation(Instance.objects.all(), expected_operation_id)
    row = target if isinstance(target, Instance) else ngfw_rows.filter(request=request, role=Instance.Role.NGFW).first()
    if row is None:
        raise ValueError("launch intent request has no NGFW instance")
    _require_current_generation(row, expected_operation_id)
    allowed_states = {
        "provision": {"pending", "provisioning"},
        "deprovision": {"ready", "paused", "failed", "destroying"},
        "start": {"paused", "failed"},
        "stop": {"ready"},
    }
    if row.status not in allowed_states[str(payload.get("operation"))]:
        raise ValueError("NGFW state does not authorize the requested operation")


def assert_provisioner_input_matches(payload: dict[str, object], operation_id: UUID) -> None:
    """Fence immutable input at effect admission, independently of failure evidence."""
    if payload.get("resource") == "vpn-profile":
        from engine.services._vpn_profile_operations import validate_vpn_operation

        validate_vpn_operation(payload, operation_id)
        return
    intent = ProvisionerLaunchIntent.objects.filter(operation_id=operation_id).first()
    if intent is not None and intent.authority_kind:
        _assert_stored_intent_matches(payload, operation_id)


def validate_provisioner_generation(
    payload: dict[str, object],
    *,
    target: Range | Instance | None = None,
    expected_operation_id: UUID | str | None = None,
) -> None:
    """Fence domain facts only; callers admitting effects first authorize policy."""
    if payload.get("resource") == "vpn-profile":
        from engine.services._vpn_profile_operations import validate_vpn_operation

        validate_vpn_operation(payload, expected_operation_id)
        return
    if "request_id" not in payload:
        _authorize_legacy_range(payload, target, expected_operation_id)
        return
    request = Request.objects.filter(request_id=UUID(str(payload["request_id"]))).first()
    if request is None:
        raise ValueError("launch intent request does not exist")
    if payload.get("resource") in {"range", "raes-range"}:
        _authorize_request_range(payload, request, target, expected_operation_id)
    else:
        _authorize_request_ngfw(payload, request, target, expected_operation_id)


def _lock_operation_target(payload: dict[str, object]) -> Range | Instance:
    """Lock and return the domain row that owns an operation generation."""
    target = lock_provisioner_target(payload)
    if not isinstance(target, (Range, Instance)):
        raise ValueError("launch intent has no operation target")
    return target


def _should_rotate_generation(row: Range | Instance, operation: str) -> bool:
    """Return whether the domain row needs a fresh operation generation."""
    if row.provisioner_operation != operation or row.provisioner_operation_id is None:
        return True
    intent = ProvisionerLaunchIntent.objects.filter(operation_id=row.provisioner_operation_id).first()
    return intent is not None and (
        intent.status == ProvisionerLaunchStatus.DLQ
        or (intent.status == ProvisionerLaunchStatus.SUCCEEDED and row.status == "failed")
    )


def _operation_identity(payload: dict[str, object], authority: dict[str, object]) -> UUID:
    """Return the stable identity of the current authorized domain operation."""
    operation = f"{payload['resource']}:{payload['operation']}"
    with transaction.atomic():
        row = _lock_operation_target(payload)
        from engine.services._launch_authority import assert_admitted_launch_binding

        assert_admitted_launch_binding(authority, row)
        authorize_provisioner_payload(payload, target=row)
        current = (
            ProvisionerLaunchIntent.objects.filter(operation_id=row.provisioner_operation_id).first()
            if row.provisioner_operation_id is not None
            else None
        )
        cleanup_transition = (
            authority.get("authority_kind") == "cleanup"
            and current is not None
            and current.authority_kind in {"principal", "warm_pool"}
            and authority.get("cleanup_origin_operation_uuid") == current.operation_id
        )
        if _should_rotate_generation(row, operation) or cleanup_transition:
            row.provisioner_operation = operation
            row.provisioner_operation_id = uuid4()
            row.save(update_fields=["provisioner_operation", "provisioner_operation_id"])
        operation_id = row.provisioner_operation_id
        assert operation_id is not None, "operation generation must be reserved"
        return operation_id


def _materialize_operation_input(payload: dict[str, object], operation_id: UUID) -> None:
    """Keep input and launch intent inside their existing shared transaction."""
    from engine.operation_inputs import materialize_operation_input

    materialize_operation_input(payload, operation_id, _lock_operation_target(payload))


def _assert_stored_intent_matches(payload: dict[str, object], operation_id: UUID) -> None:
    """Reject a re-enqueue whose composed intent differs from the immutable input.

    The ``OperationInput`` is immutable per operation generation, but immutability
    alone does not prove replay equivalence: reusing the stored input for a
    re-enqueue whose compiled plan or bindings have since changed would silently
    launch stale intent. Compose the current intent and compare its canonical
    digest to the stored one, failing closed on a mismatch (ADR-063-R2). No stored
    input (a legacy range) means there is nothing to compare.
    """
    intent = ProvisionerLaunchIntent.objects.filter(operation_id=operation_id).first()
    stored = OperationInput.objects.filter(operation_id=operation_id).first()
    if stored is None:
        if intent is not None and intent.authority_kind:
            raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
        return
    from engine.services._launch_authority import assert_input_authority_binding

    if intent is not None:
        assert_input_authority_binding(stored, intent)
    target = _lock_operation_target(payload)
    request: Request | None = getattr(target, "request", None)
    if request is None:
        return
    current = operation_input_payload(target, str(payload["resource"]), request, operation=str(payload["operation"]))
    stored_payload = (stored.envelope or {}).get("payload") or {}
    if canonical_payload_digest(current) != canonical_payload_digest(stored_payload):
        raise ValueError("re-enqueue intent does not match the stored immutable operation intent")


def enqueue_provisioner_launch(
    command: list[str], *, credential: CredentialContext | None = None, cleanup_for: UUID | None = None
) -> str:
    """Persist one durable intent per authorized operation and return its UUID."""
    payload = validate_provisioner_command(command)
    from engine.services._launch_authority import prepare_launch_authority

    authority = prepare_launch_authority(payload, credential, cleanup_for)
    return _enqueue_admitted_provisioner_launch(command, payload, authority)


def _enqueue_admitted_provisioner_launch(
    command: list[str], payload: dict[str, object], authority: dict[str, object]
) -> str:
    """Commit server-owned admission in the incumbent operation transaction."""
    from engine.services._launch_authority import assert_launch_authority_replay

    with transaction.atomic():
        operation_id = _operation_identity(payload, authority)
        from engine.services._model_allocation_launch import allocate_launch_models

        allocate_launch_models(payload, operation_id)
        existing = ProvisionerLaunchIntent.objects.filter(operation_id=operation_id).first()
        if existing is not None:
            assert_launch_authority_replay(existing, authority)
            _assert_stored_intent_matches(payload, operation_id)
            return str(existing.intent_id)
        canonical = f"{'|'.join(command)}|{operation_id}"
        idempotency_key = sha256(canonical.encode("utf-8")).hexdigest()
        existing = ProvisionerLaunchIntent.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return str(existing.intent_id)
        intent_id = uuid4()
        namespace = str(getattr(settings, "ENGINE_TASK_CLUSTER", "") or "")
        task_ref = (
            f"{namespace}/{build_idempotent_job_name(PROVISIONER_CONTAINER_NAME, str(intent_id))}" if namespace else ""
        )
        row = ProvisionerLaunchIntent.objects.create(
            intent_id=intent_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            payload=payload,
            task_ref=task_ref,
            next_attempt_at=timezone.now(),
            **({"executing_workload": _launch_workload(payload, operation_id, intent_id)} if authority else {}),
            **authority,
        )
        _materialize_operation_input(payload, operation_id)
        from functools import partial

        from engine.ecs._local import _is_local_provisioner_enabled, drain_local_intent

        if _is_local_provisioner_enabled():
            transaction.on_commit(partial(drain_local_intent, intent_id))
        return str(row.intent_id)


def _launch_workload(payload: dict[str, Any], operation_id: UUID | str, intent_id: UUID) -> dict[str, object]:
    """Bind the canonical command to its deterministic native task identity."""
    from engine.ecs._workload import provisioner_workload

    return provisioner_workload(command_from_payload({**payload, "operation_id": str(operation_id)}), str(intent_id))


def task_ref_for_intent(intent_id: str) -> str:
    """Return the provider task reference reserved for a queued intent."""
    return ProvisionerLaunchIntent.objects.only("task_ref").get(intent_id=UUID(intent_id)).task_ref
