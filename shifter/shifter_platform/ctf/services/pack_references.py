"""CTF-owned projection of records that still depend on a pack catalog id (ADR-034-R13).

Registered into :mod:`shared.pack_references` at app startup so CMS can refuse a
tenant pack removal without importing CTF (ADR-001). Two record kinds count:

- a live (not soft-deleted) event whose status is not terminal;
- a pending or running range spin-up task, whatever its event's state, because
  the provisioning loop launches without rechecking event status.

Ended events whose ranges are still live appear through those CMS ranges.
Labels are disclosed only for events the actor may read under CTF event
authority (owner, delegated staff or platform admin); others stay counted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.pack_references import PackReference, PackReferenceSet, bind_pack_reference_projection

from ..enums import EVENT_TERMINAL_STATUSES, ScheduledTaskStatus, ScheduledTaskType

if TYPE_CHECKING:
    from django.contrib.auth.models import User

_LAUNCHING_TASK_STATUSES = (ScheduledTaskStatus.PENDING.value, ScheduledTaskStatus.RUNNING.value)


def project_ctf_pack_references(scenario_id: str, limit: int, actor: User | None) -> PackReferenceSet:
    """Return at most ``limit`` CTF references to ``scenario_id`` and their total."""
    from ctf.models import CTFEvent, CTFScheduledTask

    events = CTFEvent.objects.filter(scenario_id=scenario_id).exclude(
        status__in=[status.value for status in EVENT_TERMINAL_STATUSES]
    )
    tasks = CTFScheduledTask.objects.filter(
        event__scenario_id=scenario_id,
        task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        status__in=_LAUNCHING_TASK_STATUSES,
    )
    rows = [
        ("ctf_event", event_id, status, name)
        for event_id, status, name in events.order_by("id").values_list("id", "status", "name")[:limit]
    ]
    rows += [
        ("ctf_scheduled_launch", event_id, status, name)
        for event_id, status, name in tasks.order_by("id").values_list("event_id", "status", "event__name")[
            : max(limit - len(rows), 0)
        ]
    ]
    # Blocker discovery runs under the content-removal fence. Counts remain
    # complete; optional labels are omitted rather than authorized through
    # legacy roles or remote policy while the producer holds SQL locks.
    references = tuple(PackReference(kind, status, None) for kind, event_id, status, name in rows)
    return PackReferenceSet(references, events.count() + tasks.count())


def register_ctf_pack_references() -> None:
    """Bind the CTF projection into the shared pack reference port (idempotent)."""
    bind_pack_reference_projection("ctf", project_ctf_pack_references)
