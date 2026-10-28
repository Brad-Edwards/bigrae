"""Explicit, resource-generation-bound teardown in the incumbent range outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import ProvisionerLaunchIntent, Range, RangeAuthorityChange

from uuid import UUID

from shared.operation_authority import credential_reference_fields, credential_reference_from_record

_CLEANUP_AUTHORITY_UNAVAILABLE = "Cleanup authority unavailable"


def assert_cleanup_origin(intent: ProvisionerLaunchIntent, runtime: Range) -> None:
    """Cleanup never borrows an actor's grant or authorizes another operation."""
    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority_history import cleanup_origin_record

    origin = cleanup_origin_record(intent.cleanup_origin_operation_uuid)
    if runtime.request is None or origin is None:
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)
    if (
        origin.authority_kind not in {"principal", "warm_pool"}
        or intent.payload.get("operation") != "destroy"
        or intent.payload.get("resource") not in {"range", "raes-range"}
        or runtime.resource_generation is None
        or (
            intent.payload.get("resource"),
            intent.payload.get("request_id"),
            origin.payload.get("request_id"),
            intent.authority_owner_uuid,
            intent.authority_resource_uuid,
        )
        != (
            origin.payload.get("resource"),
            str(runtime.request.request_id),
            str(runtime.request.request_id),
            origin.authority_owner_uuid,
            origin.authority_resource_uuid,
        )
        or origin.authority_binding_digest != range_runtime_binding_digest(runtime)
    ):
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)
    _assert_warm_origin(origin, runtime)
    if any(getattr(intent, name) != value for name, value in _cleanup_fields(origin).items()):
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)


def prepare_cleanup_authority(payload: dict[str, object], origin_operation_uuid: UUID) -> dict[str, object]:
    """Only server-owned teardown of a recorded generation survives revocation."""
    from engine.models import Range

    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority_history import cleanup_origin_record

    if payload.get("resource") not in {"range", "raes-range"} or payload.get("operation") != "destroy":
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)
    origin = cleanup_origin_record(origin_operation_uuid)
    runtime = (
        Range.objects.select_related("request").filter(request__request_id=UUID(str(payload.get("request_id")))).first()
    )
    if (
        runtime is None
        or runtime.request is None
        or origin is None
        or origin.authority_kind not in {"principal", "warm_pool"}
        or (origin.payload.get("resource"), origin.payload.get("request_id"))
        != (payload.get("resource"), payload.get("request_id"))
        or runtime.resource_generation is None
        or origin.authority_binding_digest != range_runtime_binding_digest(runtime)
    ):
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)
    _assert_cleanup_generation(origin, runtime, origin_operation_uuid)
    _assert_warm_origin(origin, runtime)
    return _cleanup_fields(origin)


def _cleanup_fields(origin) -> dict[str, object]:
    fields = credential_reference_fields(credential_reference_from_record(origin))
    for name in (
        "authority_action",
        "authority_target_type",
        "authority_target_uuid",
        "authority_parent_type",
        "authority_parent_uuid",
        "authority_scope_kind",
        "authority_account_uuid",
        "authority_organization_uuid",
        "authority_workspace_uuid",
        "authority_owner_uuid",
        "authority_resource_uuid",
        "authority_binding_digest",
    ):
        fields[name] = getattr(origin, name)
    fields.update({"authority_kind": "cleanup", "cleanup_origin_operation_uuid": origin.operation_id})
    return fields


def _assert_warm_origin(origin, runtime: Range) -> None:
    """A warm cleanup consumes the recorded ledger and exact creation receipt only."""
    if origin.authority_kind != "warm_pool":
        return
    from engine.models import RangeAuthorityChange, WarmRangeGeneration

    if (
        runtime.request is None
        or runtime.resource_generation is None
        or runtime.scope_kind != "installation"
        or runtime.user_id is not None
        or runtime.event_uuid is not None
        or any(value is not None for value in (runtime.account_id, runtime.organization_id, runtime.workspace_id))
        or origin.initiating_principal_kind != "service"
        or origin.initiating_principal_uuid != runtime.owner_principal_uuid
        or not WarmRangeGeneration.objects.filter(
            request_id=runtime.request.request_id,
            range=runtime,
            state__in=["retiring", "terminal"],
            claimed_by_request_id__isnull=True,
        ).exists()
    ):
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)
    receipts = list(
        RangeAuthorityChange.objects.filter(
            range=runtime,
            request_uuid=runtime.request.request_id,
            operation="create",
            authority_kind="warm_pool",
            resource_generation=runtime.resource_generation,
            authority_binding_digest=origin.authority_binding_digest,
        )[:2]
    )
    if len(receipts) != 1 or any(
        getattr(receipts[0], name) != value
        for name, value in credential_reference_fields(credential_reference_from_record(origin)).items()
    ):
        raise ValueError(_CLEANUP_AUTHORITY_UNAVAILABLE)


def _assert_cleanup_generation(
    origin: ProvisionerLaunchIntent | RangeAuthorityChange, runtime: Range, origin_operation_uuid: UUID
) -> None:
    """Keep retained transfer evidence and replayed cleanup on the same generation."""
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange

    if isinstance(origin, RangeAuthorityChange) and (
        origin.range_id != runtime.pk or origin.resource_generation != runtime.resource_generation
    ):
        raise ValueError("Cleanup generation changed")
    if runtime.provisioner_operation_id != origin_operation_uuid and not isinstance(origin, RangeAuthorityChange):
        existing = (
            ProvisionerLaunchIntent.objects.filter(operation_id=runtime.provisioner_operation_id).first()
            if runtime.provisioner_operation_id is not None
            else None
        )
        if (
            existing is None
            or existing.authority_kind != "cleanup"
            or existing.cleanup_origin_operation_uuid != origin_operation_uuid
        ):
            raise ValueError("Cleanup generation changed")
        assert_cleanup_origin(existing, runtime)
