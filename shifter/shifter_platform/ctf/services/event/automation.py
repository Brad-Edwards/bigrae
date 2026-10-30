"""Scheduler-driven automatic event start and end (CTF-1002, CTF-1004, CTF-701).

Split from ``_lifecycle`` for the python:S104 file-size budget. The automatic
start and end take the event lock, recheck the schedule under it, and commit the
organizer alert with the state change (ADR-065-R4). The scheduler command keeps
only thin task-handler wrappers around ``run_automatic_start`` and
``run_automatic_end``. Like ``_lifecycle``, the end path resolves
``complete_event`` and ``finish_ended_event`` through the ``ctf.services.event``
package at call time, so package-level patch targets keep working.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction

from ctf.enums import EventStatus
from ctf.exceptions import CTFStateError

from ._lifecycle import _transition_event

if TYPE_CHECKING:
    from ctf.models import CTFEvent, CTFScheduledTask

logger = logging.getLogger(__name__)


class AutomaticStartRefused(RuntimeError):
    """The automatic start was refused; the executor retries within the task budget."""


class AlertDeclarationPending(RuntimeError):
    """A lifecycle alert is retained on the task; the executor retries within the task budget."""


# Outcomes of the scheduler's automatic start (CTF-1004).
START_STARTED = "started"
START_REFUSED = "refused"
START_NOT_DUE = "not_due"
START_NOT_APPLICABLE = "not_applicable"
START_EXPIRED = "expired"


def start_event_automatically(event_id: UUID, *, task_id: UUID | None = None) -> str:
    """Scheduler start: lock, recheck, transition and declare the alert in one transaction.

    The event lock serializes this with manual lifecycle actions and rescheduling.
    The start time and state are rechecked under that lock. A successful start
    declares the ``event_started`` organizer alert in the same transaction, so a
    crash after commit cannot lose it. A hydration refusal keeps the event in
    registration and declares one ``event_start_refused`` alert for this schedule
    occurrence; the caller retries within its task budget. Once the end time has
    passed, the automatic start never activates the event. A refused alert
    declaration is retained on ``task_id`` for retry.
    """
    from ctf.models import CTFEvent

    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().filter(pk=event_id, deleted_at__isnull=True).first()
        outcome = _automatic_start_gate(event)
        if outcome is None and event is not None:
            outcome = _start_or_refuse(event, task_id)
    return outcome or START_NOT_APPLICABLE


def _automatic_start_gate(event: CTFEvent | None) -> str | None:
    """Return the outcome that stops an automatic start under the lock, or ``None`` to proceed."""
    from django.utils import timezone

    now = timezone.now()
    if event is None or event.status != EventStatus.REGISTRATION.value:
        outcome: str | None = START_NOT_APPLICABLE
    elif now < event.event_start:
        outcome = START_NOT_DUE
    elif now >= event.event_end:
        logger.warning("Automatic start of event %s skipped: the end time has passed", event.pk)
        outcome = START_EXPIRED
    else:
        outcome = None
    return outcome


def _start_or_refuse(event: CTFEvent, task_id: UUID | None) -> str:
    """Activate the locked event with its alert, or record a hydration refusal and its alert."""
    from ctf.services.content_hydration import assert_event_content_hydration_ready
    from ctf.services.notification import declare_or_retain_lifecycle_alert

    try:
        assert_event_content_hydration_ready(event)
    except CTFStateError as exc:
        logger.warning("Automatic start of event %s refused code=%s", event.pk, exc.code)
        declare_or_retain_lifecycle_alert(event, "event_start_refused", occurrence=event.event_start, task_id=task_id)
        return START_REFUSED
    _transition_event(event, EventStatus.ACTIVE)
    declare_or_retain_lifecycle_alert(event, "event_started", occurrence=event.event_start, task_id=task_id)
    logger.info("Automatically started CTF event %s", event.pk)
    return START_STARTED


def _settle_alerts(task: CTFScheduledTask, *, ranges_alert: dict[str, str] | None = None) -> None:
    """Retry retained lifecycle alerts and raise while any alert is still undeclared."""
    from ctf.services.notification import redeclare_pending_alerts

    remaining = redeclare_pending_alerts(task.pk)
    if remaining or (ranges_alert is not None and ranges_alert["outcome"] != "declared"):
        raise AlertDeclarationPending(f"Organizer alert pending for event {task.event_id}")


def run_automatic_start(task: CTFScheduledTask) -> dict[str, Any] | None:
    """Start the event at its scheduled start and check range readiness.

    The start and its organizer alert commit together. Range readiness is checked
    at the scheduled start even when the start is refused. A refusal raises so the
    executor retries with backoff; an early tick reschedules to the current start.
    """
    from ctf.models import CTFEvent
    from ctf.services.notification import alert_if_ranges_not_ready

    outcome = start_event_automatically(task.event_id, task_id=task.pk)
    if outcome == START_NOT_DUE:
        event_start = CTFEvent.objects.filter(pk=task.event_id).values_list("event_start", flat=True).first()
        return {"reschedule_for": event_start}
    ranges_alert = alert_if_ranges_not_ready(task.event_id)
    if outcome == START_REFUSED:
        raise AutomaticStartRefused(f"Automatic start of event {task.event_id} refused")
    _settle_alerts(task, ranges_alert=ranges_alert)
    return None


def run_automatic_end(task: CTFScheduledTask) -> None:
    """End the event at its scheduled end; the organizer alert commits with the end."""
    from django.utils import timezone

    from ctf.models import CTFEvent
    from ctf.services import event as _e

    event = CTFEvent.objects.get(pk=task.event_id)
    now = timezone.now()
    if now < event.event_end:
        logger.info(
            "Skipping stale EVENT_END task %s for event %s: event_end=%s now=%s",
            task.pk,
            event.pk,
            event.event_end,
            now,
        )
        task.cancel_if_active()
        return

    if not _e.complete_event(event, automatic=True, task_id=task.pk):
        event.refresh_from_db()
        if event.status == EventStatus.ENDED.value:
            # A replay after a crash between the end commit and its effects:
            # the end effects are idempotent, so apply them again.
            _e.finish_ended_event(event)
    _settle_alerts(task)
