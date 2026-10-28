"""Event provisioning task orchestration.

Enqueues (and coalesces) the scheduler-run SPIN_UP_RANGES task that performs the
throttled provisioning off the request thread, and projects bounded provisioning
progress for the organizer UI. The work itself lives in
:mod:`ctf.services.range.batch`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, overload
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from ctf.enums import ScheduledTaskStatus, ScheduledTaskType
from ctf.exceptions import CTFNotFoundError
from ctf.models import CTFEvent, CTFParticipant, CTFScheduledTask
from shared.log_sanitize import safe_log_value

logger = logging.getLogger(__name__)


@overload
def request_event_provisioning(
    event_id: UUID, *, source: str = "manual", actor_id: object | None = None, defer_if_busy: Literal[False] = False
) -> CTFScheduledTask:
    """Request event provisioning, optionally returning no task while another runs."""
    ...


@overload
def request_event_provisioning(
    event_id: UUID, *, source: str = "manual", actor_id: object | None = None, defer_if_busy: bool
) -> CTFScheduledTask | None:
    """Request event provisioning, optionally returning no task while another runs."""
    ...


def request_event_provisioning(
    event_id: UUID, *, source: str = "manual", actor_id: object | None = None, defer_if_busy: bool = False
) -> CTFScheduledTask | None:
    """Enqueue (or coalesce onto) a due-now SPIN_UP_RANGES task for an event.

    The organizer "provision all" action runs the throttled provisioning loop
    on the CTF scheduler instead of the request thread. Repeated clicks and a
    pre-existing scheduled spin-up coalesce onto a single runnable task:

    - a RUNNING spin-up task is reused as-is (work is already in flight);
    - a PENDING spin-up task is reused; if it is scheduled in the future it is
      pulled forward to now so the manual action takes effect immediately and
      the original task cannot fire again later;
    - otherwise a new PENDING task due now is created.

    The work itself only provisions participants without a range, so resuming or
    re-running is idempotent.

    Args:
        event_id: UUID of the event.
        source: Audit hint recorded in task metadata (e.g. "manual").
        actor_id: The initiating native actor or prepared event admission. Every
            caller must supply current range-management authority; the event
            lock fences that exact admission before any queue mutation.

    Returns:
        The active task, or None when participant admission defers a conflicting wave.

    Raises:
        CTFNotFoundError: If event doesn't exist.
        CTFPermissionError: If ``actor_id`` lacks the ``ranges`` capability.
    """
    if actor_id is None:
        raise ValueError("Range provisioning authority unavailable")
    try:
        admitted_event = CTFEvent.objects.get(pk=event_id)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(
            f"Event {event_id} not found",
            details={"event_id": str(event_id)},
        ) from None
    from ctf.enums import EventCapability
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import admit_event_actions, assert_event_capability
    from shared.operation_authority import credential_reference_fields

    admission = admit_event_actions(actor_id, admitted_event, "event.manage_ranges")
    credential = event_actor_credential(admission)
    authority_fields = credential_reference_fields(credential)
    spin_up = ScheduledTaskType.SPIN_UP_RANGES.value
    with transaction.atomic():
        # Serialize concurrent enqueues for the same event so duplicate clicks
        # cannot each create a runnable task.
        try:
            event = CTFEvent.objects.select_for_update().get(pk=event_id)
        except CTFEvent.DoesNotExist:
            raise CTFNotFoundError(
                f"Event {event_id} not found",
                details={"event_id": str(event_id)},
            ) from None

        from ctf.bridges import cms_fence_pack_reference

        assert_event_capability(admission, event, EventCapability.RANGES)
        if event.scenario_id != admitted_event.scenario_id:
            raise ValueError("Event scenario changed during provisioning admission")
        cms_fence_pack_reference(event.scenario_id)

        now = timezone.now()

        running = CTFScheduledTask.objects.filter(
            event_id=event_id,
            task_type=spin_up,
            status=ScheduledTaskStatus.RUNNING.value,
        ).first()
        if running is not None:
            return _coalesce_provision_task(running, authority_fields, source, now, defer_if_busy)

        pending = (
            CTFScheduledTask.objects.filter(
                event_id=event_id,
                task_type=spin_up,
                status=ScheduledTaskStatus.PENDING.value,
            )
            .order_by("scheduled_for")
            .first()
        )
        if pending is not None:
            return _coalesce_provision_task(pending, authority_fields, source, now, defer_if_busy)

        task = CTFScheduledTask.objects.create(
            event_id=event_id,
            task_type=spin_up,
            scheduled_for=now,
            metadata={"source": source},
            **authority_fields,
        )
    logger.info("Enqueued provision task %s for event %s", task.pk, safe_log_value(event_id))
    return task


def get_provision_progress(event_id: UUID) -> dict[str, Any]:
    """Project provisioning progress for an event.

    Returns bounded aggregate participant counts (computed from the cached
    ``range_status``) plus the active spin-up task's status, suitable for
    polling from the organizer UI. Carries no raw error text.

    Args:
        event_id: UUID of the event.

    Returns:
        ``{"counts": {...}, "task": {...} | None}``.
    """
    counts = {
        "total": 0,
        "ready": 0,
        "provisioning": 0,
        "error": 0,
        "not_assigned": 0,
        "other": 0,
    }
    statuses = CTFParticipant.objects.filter(event_id=event_id).values_list("range_status", flat=True)
    for status in statuses:
        counts["total"] += 1
        key = status or "not_assigned"
        if key in counts and key != "total":
            counts[key] += 1
        else:
            counts["other"] += 1

    active = (
        CTFScheduledTask.objects.filter(
            event_id=event_id,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            status__in=[ScheduledTaskStatus.PENDING.value, ScheduledTaskStatus.RUNNING.value],
        )
        .order_by("-scheduled_for")
        .first()
    )
    task_block = None
    if active is not None:
        task_block = {
            "id": str(active.pk),
            "status": active.status,
            "scheduled_for": active.scheduled_for.isoformat(),
        }

    return {"counts": counts, "task": task_block}


def _coalesce_provision_task(
    task: CTFScheduledTask, authority_fields: dict[str, object], source: str, now: datetime, defer_if_busy: bool
) -> CTFScheduledTask | None:
    """Reuse only the same native initiating proof and pull a pending task forward under its event lock."""
    running = task.status == ScheduledTaskStatus.RUNNING.value
    if any(getattr(task, key) != value for key, value in authority_fields.items()):
        if defer_if_busy:
            return None
        state = "running" if running else "queued"
        raise ValueError(f"Range provisioning is already {state} under another credential")
    if not running and task.scheduled_for > now:
        task.scheduled_for = now
        task.metadata = {**(task.metadata or {}), "source": source}
        task.save(update_fields=["scheduled_for", "metadata", "updated_at"])
        logger.info("Pulled scheduled spin-up task %s forward for event %s", task.pk, safe_log_value(task.event_id))
    else:
        state = "running" if running else "pending"
        logger.info("Coalescing provision request onto %s task %s", state, task.pk)
    return task
