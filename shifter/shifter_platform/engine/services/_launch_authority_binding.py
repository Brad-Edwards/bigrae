"""Local credential, ancestry and generation fences for admitted Engine launches."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from shared.operation_envelope import canonical_payload_digest

if TYPE_CHECKING:
    from engine.models import Instance, ProvisionerLaunchIntent, Range

_OPERATION_AUTHORITY_UNAVAILABLE = "Operation authority unavailable"


def range_runtime_binding_digest(row: Range | Instance) -> str:
    """Pin native resource/owner/scope/backend facts, including for cleanup."""
    from engine.models import Instance

    if isinstance(row, Instance):
        from ._device_authority import device_binding_digest

        return device_binding_digest(row)
    if row.request is None:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    return canonical_payload_digest(
        {
            "native_uuid": str(row.uuid),
            "public_uuid": str(row.cms_range_uuid),
            "request_uuid": str(row.request.request_id),
            "owner_uuid": str(row.owner_principal_uuid),
            "scope_kind": row.scope_kind,
            "account_id": row.account_id,
            "organization_id": row.organization_id,
            "workspace_id": row.workspace_id,
            "event_uuid": str(row.event_uuid),
            "range_backend": row.range_backend,
            "resource_generation": str(row.resource_generation),
        }
    )


def assert_launch_authority_binding(intent: ProvisionerLaunchIntent, runtime: Range | Instance) -> None:
    """Pure SQL-fact fence after policy evaluation, safe inside a row lock."""
    from engine.models import Instance

    if isinstance(runtime, Instance):
        from ._device_authority import assert_device_authority_binding

        assert_device_authority_binding(intent, runtime)
        return
    if intent.authority_kind not in {"principal", "cleanup", "warm_pool"} or (
        intent.authority_binding_digest != range_runtime_binding_digest(runtime)
    ):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    if intent.authority_kind == "cleanup":
        from ._launch_cleanup_authority import assert_cleanup_origin

        assert_cleanup_origin(intent, runtime)


def assert_admitted_launch_binding(fields: dict[str, object], runtime: Range | Instance) -> None:
    """Fence scope/owner/backend drift after admission and before durable commit."""
    if fields.get("authority_kind") in {"principal", "warm_pool"}:
        from shared.credentials import resolve_local_credential
        from shared.operation_authority import credential_reference_from_fields

        admitted = credential_reference_from_fields(fields)
        if resolve_local_credential(admitted) != admitted:
            raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    if fields and fields["authority_binding_digest"] != range_runtime_binding_digest(runtime):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    from engine.models import Instance

    if isinstance(runtime, Instance):
        _assert_admitted_device_binding(fields, runtime)
        return
    if fields.get("authority_kind") == "principal":
        _assert_admitted_range_scope(fields, runtime)
    if fields.get("authority_kind") == "warm_pool":
        _assert_admitted_warm_binding(fields, runtime)


def _assert_admitted_warm_binding(fields: dict[str, object], runtime: Range) -> None:
    """Fence exact warm-generation custody under the caller's existing row lock."""
    if (
        runtime.scope_kind != "installation"
        or runtime.owner_principal_uuid != fields.get("authority_owner_uuid")
        or runtime.cms_range_uuid != fields.get("authority_resource_uuid")
    ):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)


def _assert_admitted_range_scope(fields: dict[str, object], runtime: Range) -> None:
    """Keep exact native ancestry and the deferred CTF association at commit."""
    from shared.operation_authority import authority_resource_scope
    from workspaces.services import resource_scope_from_ids

    scope = authority_resource_scope(fields)
    runtime_scope = resource_scope_from_ids(
        kind=runtime.scope_kind,
        account_id=runtime.account_id,
        organization_id=runtime.organization_id,
        workspace_id=runtime.workspace_id,
    )
    if runtime.cms_range_uuid != fields.get("authority_resource_uuid") or runtime_scope != scope:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    # Creation waits for the CTF aggregate's exact participant, spare or recovery
    # association. Dispatch rechecks this same binding.
    event_parent = fields.get("authority_parent_uuid") if fields.get("authority_parent_type") == "event" else None
    if event_parent is None:
        return
    if runtime.event_uuid != event_parent or runtime.request is None or not isinstance(event_parent, UUID):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    from shared.range_workspace_aggregate import require_range_event_association

    require_range_event_association(runtime.request.request_id, event_parent)


def _assert_admitted_device_binding(fields: dict[str, object], runtime: Instance) -> None:
    """Require original principal proof or verified native device cleanup."""
    if runtime.request is None:
        raise PermissionError("Device authority unavailable")
    if runtime.request.owner_principal_uuid is not None and not fields:
        raise PermissionError("Device authority unavailable")
    if fields.get("authority_kind") == "cleanup":
        from ._device_authority import assert_device_cleanup_origin

        origin = fields["cleanup_origin_operation_uuid"]
        if not isinstance(origin, UUID):
            raise ValueError("Device cleanup origin unavailable")
        assert_device_cleanup_origin(origin, runtime)
