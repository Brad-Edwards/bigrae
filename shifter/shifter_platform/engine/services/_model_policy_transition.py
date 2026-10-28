"""Explicit model-policy revisions within an unchanged range execution generation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from shared.model_access import ContractError

if TYPE_CHECKING:
    from shared.credentials import CredentialContext

    from ._range_authority import RangeAuthorityProjection

_RANGE_UNAVAILABLE = "source.range_unavailable"


def begin_range_model_policy_change(*, request_id: UUID, expected_revision: int, actor_principal_uuid: UUID) -> int:
    """CMS authorizes the editor; commit fencing before replacement admission."""
    from engine.models import Range
    from engine.services._model_allocation_lifecycle import revoke_model_generation
    from shared.audit import AuditActorType, AuditEvent, audit_log

    with transaction.atomic():
        row = Range.objects.select_for_update().filter(request__request_id=request_id).first()
        if row is None or row.status != Range.Status.READY:
            raise ContractError(_RANGE_UNAVAILABLE)
        if type(expected_revision) is not int or row.model_source_policy_revision != expected_revision:
            raise ContractError("source.revision_conflict")
        revoke_model_generation(row.uuid, operation_id=row.provisioner_operation_id)
        row.model_source_policy_revision += 1
        row.save(update_fields=["model_source_policy_revision"])
        audit_log(
            AuditEvent(
                entity_type="range",
                entity_id=row.pk,
                action="update",
                actor_type=AuditActorType.PRINCIPAL,
                actor_id=None,
                actor_principal_uuid=actor_principal_uuid,
                context=f"model source policy revision={row.model_source_policy_revision}",
            ),
            strict=True,
        )
        return row.model_source_policy_revision


def admit_range_model_policy_change(*, request_id: UUID, expected_revision: int) -> tuple[UUID, ...]:
    """Admit replacements from the freshly prepared snapshot, then link refresh."""
    from engine.models import ModelAccessCredential, ModelAllocation, Range
    from engine.services._model_allocation_launch import allocate_launch_models

    with transaction.atomic():
        row = Range.objects.select_for_update().get(request__request_id=request_id)
        if row.status != Range.Status.READY or row.model_source_policy_revision != expected_revision:
            raise ContractError("source.revision_conflict")
        if row.provisioner_operation_id is None:
            raise ContractError(_RANGE_UNAVAILABLE)
        previous = list(
            ModelAllocation.objects.filter(
                request_id=request_id,
                operation_id=row.provisioner_operation_id,
                source_policy_revision__lt=expected_revision,
            )
            .select_related("grant")
            .order_by("source_policy_revision")
        )
        allocations = allocate_launch_models(
            {"resource": "raes-range", "operation": "provision", "request_id": str(request_id)},
            row.provisioner_operation_id,
        )
        by_role = {allocation.workload_role: allocation for allocation in allocations}
        for prior in previous:
            successor = by_role.get(prior.workload_role)
            if successor is not None:
                ModelAccessCredential.objects.filter(grant=prior.grant).exclude(refresh_hash="").update(
                    replacement_grant=successor.grant
                )
        return tuple(allocation.pk for allocation in allocations)


def revoke_range_model_access(
    *,
    request_id: UUID,
    credential: CredentialContext,
    expected_range: RangeAuthorityProjection,
    expected_operation_id: UUID | None,
    expected_policy_revision: int,
) -> int:
    """Fence a range's current model grants and dispatch leases (M09, #2126).

    CMS authorizes the range administrator; this transactionally revokes the
    current execution generation's model authorities. Success fences database
    authority but never proves provider cancellation, and it neither refunds spend
    nor releases outstanding liabilities, which remain under their original
    immutable price/account vector. Returns the number of authorities fenced.
    """
    from engine.models import Range
    from engine.services._model_allocation_lifecycle import revoke_model_generation
    from shared.audit import AuditAction, AuditActorType, AuditEvent, audit_log
    from shared.credentials import resolve_local_credential

    from ._range_authority import project_range_authority

    with transaction.atomic():
        row = Range.objects.select_for_update().filter(request__request_id=request_id).first()
        if row is None or row.provisioner_operation_id is None:
            raise ContractError(_RANGE_UNAVAILABLE)
        if (
            expected_range.request_uuid != request_id
            or project_range_authority(request_id) != expected_range
            or row.provisioner_operation_id != expected_operation_id
            or row.model_source_policy_revision != expected_policy_revision
            or resolve_local_credential(credential) != credential
        ):
            raise ContractError(_RANGE_UNAVAILABLE)
        fenced = revoke_model_generation(row.uuid, operation_id=row.provisioner_operation_id)
        audit_log(
            AuditEvent(
                entity_type="range",
                entity_id=row.pk,
                action=AuditAction.UPDATE,
                actor_type=AuditActorType.PRINCIPAL,
                actor_id=None,
                actor_principal_uuid=credential.principal.uuid,
                context=f"model access revoked authorities={fenced}",
                new_state={"model_grants_fenced": fenced},
            ),
            strict=True,
        )
        return fenced


def get_range_model_policy_status(*, request_id: UUID) -> dict[str, object]:
    """Bounded metadata for the current policy; retained allocations stay internal."""
    from engine.models import ModelAllocation, Range

    row = Range.objects.filter(request__request_id=request_id).first()
    if row is None or row.provisioner_operation_id is None:
        return {"state": "unavailable", "assignments": []}
    allocations = list(
        ModelAllocation.objects.filter(
            request_id=request_id,
            operation_id=row.provisioner_operation_id,
            source_policy_revision=row.model_source_policy_revision,
        )
        .select_related("grant")
        .order_by("workload_role")[:64]
    )
    states = {allocation.grant.state for allocation in allocations}
    state = "unavailable"
    if states == {"active"}:
        state = "active"
    elif states and states <= {"pending", "active"}:
        state = "refresh_pending"
    assignments = [
        {
            "workload": allocation.workload_role,
            "logical_alias": alias,
            "provider": shard["provider_adapter_id"],
            "model": shard["provider_model"],
            "region": shard["region"],
        }
        for allocation in allocations
        for alias, shard in allocation.snapshot["shards"].items()
    ]
    return {"state": state, "assignments": assignments}
