"""Retain an accepted transfer's original proof and current cleanup custody."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import ProvisionerLaunchIntent, Range, RangeAuthorityChange, WarmRangeGeneration

from uuid import UUID

from shared.authorization import TargetRef
from shared.authorization.policy import scope_parent
from shared.credentials import CredentialContext
from shared.operation_authority import credential_reference_fields
from shared.raes.runtime_target import is_raes_provisioning_plan

from ._launch_authority import range_runtime_binding_digest
from ._range_authority import RangeAuthorityProjection, project_range_authority


def record_range_authority_change(
    row: Range, credential: CredentialContext, operation: str, previous_digest: str
) -> None:
    """Retain original custody as bounded cleanup evidence after an authorized move."""
    from engine.models import RangeAuthorityChange

    if row.request is None:
        raise ValueError("Range custody unavailable")
    if row.scope_kind == "installation":
        if operation != "create":
            raise ValueError("Installation custody change unavailable")
        _record_warm_creation(row, credential)
        return
    projection = project_range_authority(row.request.request_id)
    if not isinstance(row.resource_generation, UUID) or not row.resource_generation.int:
        raise ValueError("Range generation unavailable")
    action, parent, target = _change_action_target(projection, operation, row.cms_range_uuid)
    RangeAuthorityChange.objects.create(
        range=row,
        request_uuid=row.request.request_id,
        resource="raes-range" if is_raes_provisioning_plan(row.range_config) else "range",
        operation=operation,
        resource_generation=row.resource_generation,
        previous_binding_digest=previous_digest,
        **credential_reference_fields(credential),
        authority_kind="principal",
        authority_action=action,
        authority_target_type=target.type,
        authority_target_uuid=target.uuid,
        authority_parent_type=parent.type,
        authority_parent_uuid=parent.uuid,
        authority_scope_kind=projection.scope.kind,
        authority_account_uuid=projection.scope.account_uuid,
        authority_organization_uuid=projection.scope.organization_uuid,
        authority_workspace_uuid=projection.scope.workspace_uuid,
        authority_owner_uuid=row.owner_principal_uuid,
        authority_resource_uuid=row.cms_range_uuid,
        authority_binding_digest=range_runtime_binding_digest(row),
    )


def _change_action_target(
    projection: RangeAuthorityProjection, operation: str, range_uuid: UUID | None
) -> tuple[str, TargetRef, TargetRef]:
    """Retain parent creation/scope authority separately from exact leaf lifecycle authority."""
    parent = projection.parent if operation == "create" else scope_parent(projection.scope)
    target = parent if operation in {"create", "scope"} else TargetRef("range", range_uuid)
    if operation == "create":
        action = "event.manage_ranges" if parent.type == "event" else f"{parent.type}.launch_range"
    else:
        action = f"{parent.type}.manage_range_scope" if operation == "scope" else "range.manage"
    return action, parent, target


def lock_warm_range_creation(request_id: UUID) -> WarmRangeGeneration:
    """Serialize creation with retirement before a native runtime can exist."""
    from engine.models import WarmRangeGeneration

    generation = WarmRangeGeneration.objects.select_for_update().get(request_id=request_id)
    if (
        generation.state != "provisioning"
        or generation.claimed_by_request_id is not None
        or generation.range_id is not None
    ):
        raise ValueError("Warm creation intent changed")
    return generation


def _record_warm_creation(row: Range, credential: CredentialContext) -> None:
    """Bind the exact service-owned generation to its retained creation receipt."""
    from engine.models import RangeAuthorityChange, WarmRangeGeneration

    from ._principal_range_create import _installation_creation_authority

    if (
        row.request is None
        or credential.principal.kind != "service"
        or row.resource_generation is None
        or row.request.owner_principal_uuid != row.owner_principal_uuid
        or row.user_id is not None
        or row.workspace_id is not None
        or row.event_uuid is not None
    ):
        raise ValueError("Warm creation custody unavailable")
    generation = WarmRangeGeneration.objects.get(request_id=row.request.request_id, state="provisioning")
    if generation.range_id is not None:
        raise ValueError("Warm creation generation changed")
    RangeAuthorityChange.objects.create(
        range=row,
        request_uuid=row.request.request_id,
        resource="raes-range" if is_raes_provisioning_plan(row.range_config) else "range",
        operation="create",
        resource_generation=row.resource_generation,
        previous_binding_digest="",
        **_installation_creation_authority(credential, row),
    )
    generation.range = row
    generation.save(update_fields=["range"])


def cleanup_origin_record(identifier: UUID | None) -> ProvisionerLaunchIntent | RangeAuthorityChange | None:
    """Resolve one retained native operation or authority change as cleanup evidence."""
    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange

    if identifier is None:
        return None
    return (
        ProvisionerLaunchIntent.objects.filter(operation_id=identifier).first()
        or RangeAuthorityChange.objects.filter(pk=identifier).first()
    )


def current_range_cleanup_origin(row: Range) -> ProvisionerLaunchIntent | RangeAuthorityChange | None:
    """Select cleanup evidence matching the current runtime generation and custody."""
    from engine.models import ProvisionerLaunchIntent

    if row.resource_generation is None:
        return None
    current = (
        ProvisionerLaunchIntent.objects.filter(operation_id=row.provisioner_operation_id).first()
        if row.provisioner_operation_id is not None
        else None
    )
    digest = range_runtime_binding_digest(row)
    if current is not None and current.authority_kind == "cleanup":
        origin = cleanup_origin_record(current.cleanup_origin_operation_uuid)
    elif (
        current is not None
        and current.authority_kind in {"principal", "warm_pool"}
        and current.authority_binding_digest == digest
    ):
        origin = current
    else:
        origin = (
            row.authority_changes.filter(resource_generation=row.resource_generation, authority_binding_digest=digest)
            .order_by("-accepted_at", "-id")
            .first()
        )
    return origin
