"""CMS-owned state handling for the one-time identity hard cut."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from shared.operation_authority import credential_reference_from_record


def identity_cutover_resource_disposition() -> tuple[int, int]:
    """Return retained resource count and rows with no reconstructable owner."""
    from cms.models import RangeInstance, Request

    return (
        Request._base_manager.count() + RangeInstance._base_manager.count(),
        Request._base_manager.filter(owner_principal_uuid__isnull=True, user__isnull=True).count()
        + RangeInstance._base_manager.filter(owner_principal_uuid__isnull=True, user_id__isnull=True).count(),
    )


def identity_cutover_contract_gaps() -> int:
    """Count persisted model-authority JSON that the native contract cannot prove."""
    from cms.models import RangeInstance
    from shared.model_access.reservation import ModelLaunchScope
    from shared.model_access.sources import ModelSourceSponsorship

    gaps = 0
    rows = RangeInstance._base_manager.values_list("model_source_sponsorship", "model_launch_scope")
    for sponsorship, launch_scope in rows.iterator():
        try:
            if sponsorship is not None:
                ModelSourceSponsorship.model_validate(sponsorship)
            if launch_scope is not None:
                ModelLaunchScope.model_validate(launch_scope)
        except ValueError:
            gaps += 1
    return gaps


def pending_identity_cutover_work() -> tuple[int, int]:
    """Count pending CMS work and rows lacking a complete initiator reference."""
    from cms.models import AgentUpload

    pending = missing = 0
    for row in AgentUpload.objects.filter(state__in=("issued", "finalizing")).iterator():
        pending += 1
        try:
            if not credential_reference_from_record(row).ceiling.actions:
                raise ValueError("empty ceiling")
        except ValueError:
            missing += 1
    return pending, missing


def apply_identity_cutover_resources(
    mappings: Mapping[int, tuple[int, int | None, int | None]], principal_by_user: Mapping[int, UUID]
) -> None:
    """Bind retained CMS resources to planned account and principal facts."""
    from cms.models import RangeInstance, Request

    for workspace_id, (account_id, organization_id, retained_workspace_id) in mappings.items():
        for model in (Request, RangeInstance):
            model._base_manager.filter(workspace_id=workspace_id).update(
                scope_kind="account",
                account_id=account_id,
                organization_id=organization_id,
                workspace_id=retained_workspace_id,
            )
    for user_id, principal_uuid in principal_by_user.items():
        Request._base_manager.filter(owner_principal_uuid__isnull=True, user_id=user_id).update(
            owner_principal_uuid=principal_uuid
        )
        RangeInstance._base_manager.filter(owner_principal_uuid__isnull=True, user_id=user_id).update(
            owner_principal_uuid=principal_uuid
        )


def verify_identity_cutover_hierarchy(plan: Mapping[str, object]) -> None:
    """Verify tenancy-owned hierarchy facts through the CMS control-plane seam."""
    from workspaces.services import verify_identity_cutover_hierarchy as verify_workspace_hierarchy

    verify_workspace_hierarchy(plan)
