"""Explicit, resource-generation-bound teardown in the incumbent range outbox."""

from uuid import UUID

from shared.operation_authority import credential_reference_fields, credential_reference_from_record


def assert_cleanup_origin(intent, runtime) -> None:
    """Cleanup never borrows an actor's grant or authorizes another operation."""
    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority_history import cleanup_origin_record

    origin = cleanup_origin_record(intent.cleanup_origin_operation_uuid)
    if (
        origin is None
        or origin.authority_kind != "principal"
        or intent.payload.get("resource") != origin.payload.get("resource")
        or intent.payload.get("operation") != "destroy"
        or intent.payload.get("resource") not in {"range", "raes-range"}
        or intent.payload.get("request_id") != str(runtime.request.request_id)
        or origin.payload.get("request_id") != str(runtime.request.request_id)
        or runtime.resource_generation is None
        or origin.authority_binding_digest != range_runtime_binding_digest(runtime)
        or intent.authority_owner_uuid != origin.authority_owner_uuid
        or intent.authority_resource_uuid != origin.authority_resource_uuid
    ):
        raise ValueError("Cleanup authority unavailable")


def prepare_cleanup_authority(payload: dict[str, object], origin_operation_uuid: UUID) -> dict[str, object]:
    """Only server-owned teardown of a recorded generation survives revocation."""
    from engine.models import ProvisionerLaunchIntent, Range, RangeAuthorityChange

    from ._launch_authority import range_runtime_binding_digest
    from ._range_authority_history import cleanup_origin_record

    if payload.get("resource") not in {"range", "raes-range"} or payload.get("operation") != "destroy":
        raise ValueError("Cleanup authority unavailable")
    origin = cleanup_origin_record(origin_operation_uuid)
    runtime = (
        Range.objects.select_related("request").filter(request__request_id=UUID(str(payload.get("request_id")))).first()
    )
    if (
        origin is None
        or runtime is None
        or origin.authority_kind != "principal"
        or origin.payload.get("resource") != payload.get("resource")
        or origin.payload.get("request_id") != payload.get("request_id")
        or runtime.resource_generation is None
        or origin.authority_binding_digest != range_runtime_binding_digest(runtime)
    ):
        raise ValueError("Cleanup authority unavailable")
    if isinstance(origin, RangeAuthorityChange) and (
        origin.range_id != runtime.pk or origin.resource_generation != runtime.resource_generation
    ):
        raise ValueError("Cleanup generation changed")
    if runtime.provisioner_operation_id != origin_operation_uuid and not isinstance(origin, RangeAuthorityChange):
        existing = ProvisionerLaunchIntent.objects.filter(operation_id=runtime.provisioner_operation_id).first()
        if (
            existing is None
            or existing.authority_kind != "cleanup"
            or existing.cleanup_origin_operation_uuid != origin_operation_uuid
        ):
            raise ValueError("Cleanup generation changed")
        assert_cleanup_origin(existing, runtime)
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
