"""Engine-owned state handling for the one-time identity hard cut."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from shared.operation_authority import credential_reference_from_record


def identity_cutover_resource_disposition() -> tuple[int, int]:
    """Return retained resource count and rows with no reconstructable owner."""
    from engine.models import Range, Request

    models = (Request, Range)
    return (
        sum(model._base_manager.count() for model in models),
        sum(
            model._base_manager.filter(owner_principal_uuid__isnull=True, user_id__isnull=True).count()
            for model in models
        ),
    )


def identity_cutover_contract_gaps() -> int:
    """Count source revisions whose JSON uses a retired authority contract."""
    from engine.models import ModelSourceRevision
    from shared.model_access.sources import ModelSourceConfiguration

    gaps = 0
    for configuration in ModelSourceRevision.objects.values_list("configuration", flat=True).iterator():
        try:
            ModelSourceConfiguration.model_validate(configuration)
        except ValueError:
            gaps += 1
    return gaps


def pending_identity_cutover_work() -> tuple[int, int]:
    """Count pending Engine work and rows lacking a complete initiator reference."""
    from engine.models import (
        PreparationOperation,
        ProvisionerLaunchIntent,
        RuntimePluginInstallation,
        VpnAccessProfile,
    )

    querysets = (
        ProvisionerLaunchIntent.objects.filter(status__in=("PENDING", "RUNNING")),
        PreparationOperation.objects.filter(state__in=("queued", "running", "verifying")),
        RuntimePluginInstallation.objects.filter(state="checking"),
        VpnAccessProfile.objects.filter(state="pending"),
    )
    pending = missing = 0
    for queryset in querysets:
        for row in queryset.iterator():
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
    """Bind retained runtime resources to planned account and principal facts."""
    from engine.models import Range, Request

    for workspace_id, (account_id, organization_id, retained_workspace_id) in mappings.items():
        for model in (Request, Range):
            model._base_manager.filter(workspace_id=workspace_id).update(
                scope_kind="account",
                account_id=account_id,
                organization_id=organization_id,
                workspace_id=retained_workspace_id,
            )
    for model in (Request, Range):
        for user_id, principal_uuid in principal_by_user.items():
            model._base_manager.filter(owner_principal_uuid__isnull=True, user_id=user_id).update(
                owner_principal_uuid=principal_uuid
            )
