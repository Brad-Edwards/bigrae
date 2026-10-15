"""Move an existing range reservation under both native workspace mutexes."""

from uuid import UUID

from django.db import transaction

from workspaces.models import QUOTA_RESOURCE_CONCURRENT_RANGES, Workspace, WorkspaceQuotaReservation

from ._quota import (
    WorkspaceQuotaAuditContext,
    WorkspaceQuotaRejected,
    _current_usage,
    _error,
    _evaluate,
    _policy_for,
    _record_decision,
    release_workspace_concurrent_range,
)


def move_workspace_range_reservation(
    source_workspace_id: int | None,
    target_workspace_id: int | None,
    correlation: UUID,
    audit: WorkspaceQuotaAuditContext,
) -> None:
    """Re-evaluate destination capacity, including a return to a released slot.

    CMS supplies the persisted source and an independently authorized target.
    This is quota accounting only; it grants no permission to move the range.
    """
    if source_workspace_id == target_workspace_id:
        return
    key = str(correlation)
    with transaction.atomic():
        ids = {value for value in (source_workspace_id, target_workspace_id) if value is not None}
        locked = {row.pk: row for row in Workspace.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
        if target_workspace_id is not None:
            target = locked.get(target_workspace_id)
            if target is None or target.archived_at is not None:
                raise _error("quota_workspace_not_found", "Workspace unavailable")
            resource = QUOTA_RESOURCE_CONCURRENT_RANGES
            existing = WorkspaceQuotaReservation.objects.filter(
                workspace_id=target_workspace_id, resource=resource, correlation_key=key
            ).first()
            delta = 0 if existing is not None and existing.released_at is None else 1
            verdict = _evaluate(
                resource,
                _current_usage(target_workspace_id, resource),
                _policy_for(target_workspace_id, resource),
                delta=delta,
            )
            if verdict.rejected:
                raise WorkspaceQuotaRejected(verdict, target_workspace_id)
            if existing is None:
                WorkspaceQuotaReservation.objects.create(
                    workspace_id=target_workspace_id, resource=resource, correlation_key=key
                )
            elif existing.released_at is not None:
                existing.released_at = None
                existing.save(update_fields=["released_at"])
            _record_decision(target_workspace_id, verdict, key, audit, delta=delta)
        if source_workspace_id is not None:
            release_workspace_concurrent_range(source_workspace_id, correlation)
