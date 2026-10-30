"""Organizer alerts on the scoped communication ledger (ADR-065-R4, #528).

An organizer alert is a trusted system notice addressed to the event's current
owner and live co-organizers through the internal ``event_organizers`` audience.
It is declared durably in the caller's transaction (a scheduled intent plus its
release task), so the alert commits with the state change that caused it. The
release task resolves recipients against live native policy; the delivery worker
and the organizer inbox recheck them. Alerts never become participant broadcasts.

Capacity and provisioning-failure alerts remain unavailable: their payloads are
operator diagnostics, not organizer content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from django.db import transaction
from django.utils import timezone

from ctf.communication_contracts import MAX_SUBJECT_CODEPOINTS, ORGANIZER_ALERT_ACTIONS, ORGANIZER_AUDIENCE_KIND
from ctf.exceptions import CTFCommunicationError

if TYPE_CHECKING:
    from ctf.models import CommunicationCampaign, CTFEvent

logger = logging.getLogger(__name__)

_EVENT_READ = "event.read"
_MANAGE_RANGES = "event.manage_ranges"

# Closed alert vocabulary: kind -> (subject prefix, required policy action).
ALERT_KINDS: dict[str, tuple[str, str]] = {
    "event_started": ("Event started automatically", _EVENT_READ),
    "event_start_refused": ("Event could not start automatically", _EVENT_READ),
    "ranges_not_ready": ("Participant ranges are not ready", _MANAGE_RANGES),
    "event_ended": ("Event ended automatically", _EVENT_READ),
}


# Fixed alert text for the kinds a lifecycle transition declares.
ALERT_BODIES: dict[str, str] = {
    "event_started": "The event started at its scheduled start time. Participants can now submit flags.",
    "event_start_refused": (
        "The event did not start at its scheduled start time because its content is not ready. "
        "The system tries again automatically. Restore or refresh the event content."
    ),
    "event_ended": "The event ended at its scheduled end time. Submissions are closed.",
}


@dataclass(frozen=True, slots=True)
class OrganizerAlert:
    """One organizer alert occurrence; text is fixed sentences and counts only."""

    kind: str
    occurrence: str
    subject: str
    body: str
    action: str

    def __post_init__(self) -> None:
        if self.action not in ORGANIZER_ALERT_ACTIONS:
            raise ValueError("Unsupported organizer alert action")


def _subject(prefix: str, event: CTFEvent) -> str:
    """Name the event in the subject when it fits the plain-text subject bound."""
    name = " ".join(str(event.name or "").split())
    subject = f"{prefix}: {name}" if name else prefix
    return subject if len(subject) <= MAX_SUBJECT_CODEPOINTS else prefix


def alert_for(event: CTFEvent, kind: str, *, occurrence: datetime, body: str) -> OrganizerAlert:
    """Build one alert of a closed ``kind`` for an authored schedule occurrence."""
    prefix, action = ALERT_KINDS[kind]
    return OrganizerAlert(
        kind=kind, occurrence=occurrence.isoformat(), subject=_subject(prefix, event), body=body, action=action
    )


def _channels() -> list[str]:
    """Select in-app always, and email only when this deployment can deliver it."""
    from ctf.services.communication.adapters import registered_channels

    return ["in_app", "email"] if "email" in registered_channels() else ["in_app"]


def declare_organizer_alert(event: CTFEvent, alert: OrganizerAlert) -> dict[str, str]:
    """Declare one durable organizer alert inside the caller's transaction.

    The campaign identity is derived from the event, alert kind and occurrence,
    so a retry or a replayed scheduler task collapses onto the first declaration.
    A declaration failure is logged with its bounded code and never fails the
    caller's state change.
    """
    from ctf.models import CommunicationCampaign, CTFEvent
    from ctf.services.communication import AdmissionActor, CampaignDraft, create_system_event_campaign
    from ctf.services.communication.scheduling import schedule_declaration

    identity = uuid5(NAMESPACE_URL, f"shifter:ctf-organizer-alert:{event.pk}:{alert.kind}:{alert.occurrence}")
    try:
        with transaction.atomic():
            locked = CTFEvent.objects.select_for_update(of=("self",)).select_related("created_by").get(pk=event.pk)
            campaign = CommunicationCampaign.objects.filter(pk=identity).first()
            if campaign is None:
                campaign = create_system_event_campaign(
                    locked,
                    CampaignDraft(
                        title=alert.subject,
                        origin="system_milestone",
                        target_event_ids=[locked.pk],
                        audience_spec={
                            "kind": ORGANIZER_AUDIENCE_KIND,
                            "event_ids": [str(locked.pk)],
                            "action": alert.action,
                        },
                        trigger_spec={"kind": "absolute_time", "due_at": timezone.now().isoformat()},
                        channels=_channels(),
                        subject=alert.subject,
                        body=alert.body,
                        notice_kind="organizer_alert",
                    ),
                    campaign_id=identity,
                    authoring_rate_limited=False,
                )
            intent = schedule_declaration(
                campaign,
                due_at=_due_at(campaign),
                occurrence_key="alert",
                actor=AdmissionActor(system=True),
            )
    except CTFCommunicationError as exc:
        logger.warning("Organizer alert unavailable: event=%s kind=%s code=%s", event.pk, alert.kind, exc.code)
        return {"campaign_id": str(identity), "outcome": "unavailable"}
    return {"campaign_id": str(campaign.pk), "intent_id": str(intent.pk), "outcome": "declared"}


def declare_or_retain_lifecycle_alert(
    event: CTFEvent, kind: str, *, occurrence: datetime, task_id: UUID | None
) -> dict[str, str]:
    """Declare a fixed-text lifecycle alert, or retain it on the claimed task for retry.

    Runs inside the caller's state-change transaction. When the declaration is
    refused, the pending alert is written to the scheduler task in that same
    transaction, so the committed state change cannot lose it; the task's
    existing backoff then retries it through ``redeclare_pending_alerts``.
    """
    result = declare_organizer_alert(event, alert_for(event, kind, occurrence=occurrence, body=ALERT_BODIES[kind]))
    if result["outcome"] != "declared" and task_id is not None:
        from ctf.models import CTFScheduledTask

        task = CTFScheduledTask.objects.select_for_update().get(pk=task_id)
        pending = list((task.metadata or {}).get("pending_alerts", []))
        entry = {"kind": kind, "occurrence": occurrence.isoformat()}
        if entry not in pending:
            pending.append(entry)
        CTFScheduledTask.objects.filter(pk=task_id).update(
            metadata={**(task.metadata or {}), "pending_alerts": pending}, updated_at=timezone.now()
        )
    return result


def redeclare_pending_alerts(task_id: UUID) -> int:
    """Retry the lifecycle alerts retained on a scheduler task; return how many remain.

    The occurrence is the authored schedule instant recorded with the state change,
    so a recovered alert keeps its original identity and a replay collapses onto it.
    """
    from ctf.models import CTFEvent, CTFScheduledTask

    with transaction.atomic():
        task = CTFScheduledTask.objects.select_for_update().get(pk=task_id)
        pending = list((task.metadata or {}).get("pending_alerts", []))
        if not pending:
            return 0
        event = CTFEvent.objects.get(pk=task.event_id)
        remaining = []
        for entry in pending:
            kind, occurrence = entry["kind"], datetime.fromisoformat(entry["occurrence"])
            alert = alert_for(event, kind, occurrence=occurrence, body=ALERT_BODIES[kind])
            if declare_organizer_alert(event, alert)["outcome"] != "declared":
                remaining.append(entry)
        CTFScheduledTask.objects.filter(pk=task_id).update(
            metadata={**(task.metadata or {}), "pending_alerts": remaining}, updated_at=timezone.now()
        )
    return len(remaining)


def alert_if_ranges_not_ready(event_id: UUID) -> dict[str, str] | None:
    """At the scheduled start, declare a late-range alert with cohort counts (CTF-1002).

    The count owner is the range progress projection over the eligible cohort. An
    empty cohort or a fully ready cohort declares nothing. A cancelled event, or a
    check before the start time, declares nothing. The alert identity pins the
    schedule occurrence, so repeated checks collapse onto the first declaration.
    """
    from ctf.enums import EventStatus
    from ctf.models import CTFEvent
    from ctf.services.range.tasks import get_provision_progress

    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().filter(pk=event_id, deleted_at__isnull=True).first()
        if event is None or event.status == EventStatus.CANCELLED.value or timezone.now() < event.event_start:
            return None
        counts = get_provision_progress(event.pk)["counts"]
        total, ready = counts["total"], counts["ready"]
        if total == 0 or ready >= total:
            return None
        body = (
            f"At the scheduled start time, {ready} of {total} participant ranges were ready. "
            f"{total - ready} were not ready. Provisioning continues. "
            "Open the Ranges tab on the event monitoring page for details."
        )
        return declare_organizer_alert(
            event, alert_for(event, "ranges_not_ready", occurrence=event.event_start, body=body)
        )


def _due_at(campaign: CommunicationCampaign) -> datetime:
    """Return the campaign's authored absolute due time."""
    return datetime.fromisoformat(campaign.trigger_spec["due_at"])


def _unavailable(event_id: UUID, *args: object, **kwargs: object) -> dict[str, str]:
    """Report an unsupported organizer alert as an unavailable dependency."""

    logger.info("Organizer notification unavailable: event=%s reason=no_supported_alert_content", event_id)
    return {"outcome": "channel_unavailable"}


notify_organizer_provision_failure = _unavailable
notify_organizer_capacity_outcome = _unavailable
