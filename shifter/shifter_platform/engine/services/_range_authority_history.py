"""Retain an accepted transfer's original proof and current cleanup custody."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import ProvisionerLaunchIntent, Range, RangeAuthorityChange

from uuid import UUID

from shared.authorization import TargetRef
from shared.authorization.policy import scope_parent
from shared.credentials import CredentialContext
from shared.operation_authority import credential_reference_fields
from shared.raes.runtime_target import is_raes_provisioning_plan

from ._launch_authority import range_runtime_binding_digest
from ._range_authority import project_range_authority


def record_range_authority_change(
    row: Range, credential: CredentialContext, operation: str, previous_digest: str
) -> None:
    """Retain original custody as bounded cleanup evidence after an authorized move."""
    from engine.models import RangeAuthorityChange

    if row.request is None:
        raise ValueError("Range custody unavailable")
    projection = project_range_authority(row.request.request_id)
    if not isinstance(row.resource_generation, UUID) or not row.resource_generation.int:
        raise ValueError("Range generation unavailable")
    parent = scope_parent(projection.scope)
    target = parent if operation == "scope" else TargetRef("range", row.cms_range_uuid)
    action = f"{parent.type}.manage_range_scope" if operation == "scope" else "range.manage"
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
    elif current is not None and current.authority_kind == "principal" and current.authority_binding_digest == digest:
        origin = current
    else:
        origin = (
            row.authority_changes.filter(resource_generation=row.resource_generation, authority_binding_digest=digest)
            .order_by("-accepted_at", "-id")
            .first()
        )
    return origin
