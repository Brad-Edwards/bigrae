"""Authorized read projections for the communications console (#2052).

Every function here reads existing ledger rows; none writes, reserves capacity, or
calls a transport. Public functions authorize the complete campaign target set through the owning
read-admission boundary; recipient projections additionally require exact-event
roster authority. Projections carry bounded identifiers, counts, and display
names only: never a delivery coordinate, a body outside the reviewed preview, or
another event's roster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from ctf.enums import ScheduledTaskType
from ctf.enums_communication import DeliveryStatus, IntentStatus
from ctf.exceptions import CTFCommunicationError
from ctf.models import (
    CommunicationCampaign,
    CommunicationIntent,
    CTFScheduledTask,
    DeliveryAttempt,
    MessageRevision,
    RecipientSnapshot,
)
from ctf.services.communication.adapters import registered_channels
from ctf.services.communication.admission import AdmissionActor
from ctf.services.communication.audience import recipient_queryset
from ctf.services.communication.backpressure import max_audience
from ctf.services.communication.read_admission import authorized_campaign
from ctf.services.communication.targets import list_target_options, list_targets

# Upper bound on timeline rows per source; the console reports truncation.
MAX_TIMELINE_ROWS = 100

_NOT_FOUND = "CTF_COMMUNICATION_NOT_FOUND"


def _unavailable() -> CTFCommunicationError:
    """Return the one opaque denial shared by missing and unauthorized lookups."""
    return CTFCommunicationError("Unavailable", code=_NOT_FOUND)


def _due_at(campaign: CommunicationCampaign) -> datetime | None:
    """Return the campaign's authored UTC due instant, if it is timed."""
    raw = campaign.trigger_spec.get("due_at")
    return datetime.fromisoformat(raw) if raw else None


def _local(instant: datetime | None, zone: str) -> str | None:
    """Render ``instant`` as ISO 8601 in an event's IANA zone; an unknown zone yields ``None``."""
    if instant is None:
        return None
    try:
        return instant.astimezone(ZoneInfo(zone)).isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return None


def trigger_projection(campaign: CommunicationCampaign) -> dict[str, Any]:
    """Return the closed trigger kind and its UTC due instant."""
    return {"kind": campaign.trigger_spec.get("kind"), "due_at": _due_at(campaign)}


def _preview(campaign: CommunicationCampaign, revision_id: UUID) -> dict[str, Any]:
    """Project one reviewed revision and the current audience estimate.

    Counts aggregate the release audience query per event without loading
    participants. They are estimates at ``observed_at``; release resolves the
    final audience. Nothing is written.
    """
    revision = MessageRevision.objects.filter(pk=revision_id, campaign=campaign).first()
    if revision is None:
        raise _unavailable()
    targets = list(campaign.target_events.order_by("name", "pk"))
    counts = {
        row["event_id"]: row["recipients"]
        for row in recipient_queryset({event.pk for event in targets}, campaign.audience_spec)
        .values("event_id")
        .annotate(recipients=Count("id"))
    }
    due_at = _due_at(campaign)
    available = registered_channels()
    total = sum(counts.values())
    return {
        "campaign_id": campaign.pk,
        "revision": revision,
        "channels": [{"channel": channel, "available": channel in available} for channel in campaign.channels],
        "acknowledgement_policy": campaign.acknowledgement_policy,
        "trigger": trigger_projection(campaign),
        "events": [
            {
                "event_id": event.pk,
                "name": event.name,
                "event_timezone": event.event_timezone,
                "recipient_count": counts.get(event.pk, 0),
                "local_due_at": _local(due_at, event.event_timezone),
            }
            for event in targets
        ],
        "recipient_total": total,
        "audience_cap": max_audience(),
        "exceeds_audience_cap": total > max_audience(),
        "observed_at": timezone.now(),
    }


def _empty_channel(channel: str) -> dict[str, Any]:
    """Return a zero-filled per-channel status row."""
    return {"channel": channel, **{status.value: 0 for status in DeliveryStatus}}


def _schedule_states(intent_ids: list[UUID]) -> dict[str, str]:
    """Return the latest release-task status for each scheduled intent."""
    states: dict[str, str] = {}
    tasks = CTFScheduledTask.objects.filter(
        task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value,
        metadata__intent_id__in=[str(intent_id) for intent_id in intent_ids],
    ).order_by("created_at")
    for task in tasks:
        states[task.metadata["intent_id"]] = task.status
    return states


def _attempt_counts(intent_ids: list[UUID]) -> dict[tuple[UUID, UUID, str], dict[str, int]]:
    """Count delivery commands by (intent, event, channel) and status in one grouped query."""
    counts: dict[tuple[UUID, UUID, str], dict[str, int]] = {}
    for attempt in (
        DeliveryAttempt.objects.filter(intent_id__in=intent_ids)
        .values("intent_id", "snapshot__event_id", "channel", "status")
        .annotate(total=Count("id"))
    ):
        key = (attempt["intent_id"], attempt["snapshot__event_id"], attempt["channel"])
        counts.setdefault(key, {})[attempt["status"]] = attempt["total"]
    return counts


def _receipt_counts(intent_ids: list[UUID]) -> dict[tuple[UUID, UUID], dict[str, int]]:
    """Count recipients and inbox receipts by (intent, event); one receipt per snapshot."""
    return {
        (row["intent_id"], row["event_id"]): row
        for row in RecipientSnapshot.objects.filter(intent_id__in=intent_ids)
        .values("intent_id", "event_id")
        .annotate(
            recipients=Count("id"),
            inbox_available=Count("receipt"),
            read=Count("receipt__read_at"),
            acknowledged=Count("receipt__acknowledged_at"),
        )
    }


_RECEIPT_FIELDS = ("recipients", "inbox_available", "read", "acknowledged")


def _occurrence_events(
    intent_id: UUID,
    targets: Sequence[Mapping[str, Any]],
    channels: list[str],
    attempts: dict[tuple[UUID, UUID, str], dict[str, int]],
    receipts: dict[tuple[UUID, UUID], dict[str, int]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return per-event rows and the occurrence totals for one intent."""
    events = []
    totals: dict[str, Any] = dict.fromkeys(_RECEIPT_FIELDS, 0)
    total_channels = {channel: _empty_channel(channel) for channel in channels}
    for target in targets:
        counts = receipts.get((intent_id, target["id"]), {})
        row: dict[str, Any] = {"event_id": target["id"], "name": target["name"], "channels": []}
        for field in _RECEIPT_FIELDS:
            row[field] = counts.get(field, 0)
            totals[field] += row[field]
        for channel in channels:
            channel_row = _empty_channel(channel)
            for status_value, total in attempts.get((intent_id, target["id"], channel), {}).items():
                channel_row[status_value] = total
                total_channels[channel][status_value] += total
            row["channels"].append(channel_row)
        events.append(row)
    totals["channels"] = list(total_channels.values())
    return events, totals


def _delivery_report(campaign: CommunicationCampaign, *, limit: int, offset: int) -> dict[str, Any]:
    """Aggregate delivery state per occurrence, event, and channel.

    Delivery commands and receipts are counted in separate grouped queries, so a
    receipt is never multiplied by the number of channels. Each command keeps its
    own truthful status; nothing here is a persisted aggregate.
    """
    page = list(
        CommunicationIntent.objects.filter(campaign=campaign)
        .select_related("revision")
        .order_by("-created_at", "-pk")[offset : offset + limit + 1]
    )
    intents = page[:limit]
    ids = [intent.pk for intent in intents]
    targets = list(campaign.target_events.order_by("name", "pk").values("id", "name"))
    channels = list(campaign.channels)
    attempts, receipts = _attempt_counts(ids), _receipt_counts(ids)
    schedule = _schedule_states([intent.pk for intent in intents if intent.due_at is not None])
    results = []
    for intent in intents:
        events, totals = _occurrence_events(intent.pk, targets, channels, attempts, receipts)
        results.append(
            {
                "intent_id": intent.pk,
                "status": intent.status,
                "trigger_kind": intent.trigger_kind,
                "revision_number": intent.revision.revision_number,
                "due_at": intent.due_at,
                "released_at": intent.released_at,
                "schedule_status": schedule.get(str(intent.pk)),
                "events": events,
                "totals": totals,
            }
        )
    return {"results": results, "next_offset": offset + limit if len(page) > limit else None}


def _recipient_row(snapshot: RecipientSnapshot) -> dict[str, Any]:
    """Project one snapshot: name and team only for a live participant, never a coordinate."""
    participant = snapshot.participant
    live = participant if participant is not None and participant.deleted_at is None else None
    team = live.team if live else None
    receipt = getattr(snapshot, "receipt", None)
    return {
        "participant_id": snapshot.participant_public_id,
        "name": live.name if live else None,
        "team": team.name if team else None,
        "removed": live is None,
        "inbox_available": receipt is not None,
        "read_at": getattr(receipt, "read_at", None),
        "acknowledged_at": getattr(receipt, "acknowledged_at", None),
        "channels": [
            {"channel": attempt.channel, "status": attempt.status}
            for attempt in sorted(snapshot.delivery_attempts.all(), key=lambda a: a.channel)
        ],
    }


def _recipient_rows(
    campaign: CommunicationCampaign, *, intent_id: UUID, event_id: UUID, limit: int, offset: int
) -> dict[str, Any]:
    """Return minimal per-recipient state for one occurrence and one target event.

    The actor must also hold the ``participants`` capability on that event. Rows
    carry the participant name and team name only; a removed participant shows
    no name. Delivery coordinates are never decrypted.
    """
    intent = CommunicationIntent.objects.filter(pk=intent_id, campaign=campaign).first()
    event = campaign.target_events.filter(pk=event_id).first()
    if intent is None or event is None:
        raise _unavailable()
    snapshots = list(
        RecipientSnapshot.objects.filter(intent=intent, event=event)
        .select_related("participant", "participant__team", "receipt")
        .prefetch_related("delivery_attempts")
        .order_by("participant_public_id")[offset : offset + limit + 1]
    )
    rows = [_recipient_row(snapshot) for snapshot in snapshots[:limit]]
    return {"results": rows, "next_offset": offset + limit if len(snapshots) > limit else None}


def _actor(
    user_id: int | None,
    token_id: int | None,
    labels: dict[int, str],
    via: str = "",
    *,
    principal_uuid: UUID | None = None,
) -> dict[str, Any] | None:
    """Retain native attribution without guessing a human from a service contact."""
    if user_id is None and principal_uuid is None:
        return None
    return {
        "label": labels.get(user_id, "") if user_id is not None else "Principal",
        "via": via or ("api_token" if token_id is not None else "session" if user_id is not None else "credential"),
        "principal_uuid": principal_uuid,
    }


def _entry(kind: str, at: datetime, actor: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    """Build one timeline entry with every optional field present."""
    base = {"kind": kind, "at": at, "actor": actor, "revision_number": None, "intent_id": None}
    return {**base, "status": None, "due_at": None, **extra}


def _intent_entries(intent: CommunicationIntent, labels: dict[int, str]) -> list[dict[str, Any]]:
    """Return the scheduled, released, and terminal entries for one occurrence."""
    actor = _actor(intent.actor_user_id, intent.actor_token_id, labels, principal_uuid=intent.initiating_principal_uuid)
    extra = {
        "intent_id": intent.pk,
        "revision_number": intent.revision.revision_number,
        "due_at": intent.due_at,
        "status": intent.status,
    }
    entries = []
    if intent.due_at is not None:
        entries.append(_entry("scheduled", intent.created_at, actor, **extra))
    if intent.released_at is not None:
        # A timed occurrence is released by the scheduler on the author's authority.
        via = "scheduler" if intent.due_at is not None else ""
        release_actor = _actor(
            intent.actor_user_id,
            intent.actor_token_id,
            labels,
            via=via,
            principal_uuid=intent.initiating_principal_uuid,
        )
        entries.append(_entry("released", intent.released_at, release_actor, **extra))
    if intent.status in (IntentStatus.EXPIRED.value, IntentStatus.FENCED.value):
        entries.append(_entry(intent.status, intent.updated_at, None, **extra))
    return entries


def _activity_timeline(campaign: CommunicationCampaign) -> dict[str, Any]:
    """Return the body-free campaign timeline built from ledger attribution.

    Entries cover creation, each revision, each scheduled or released occurrence,
    expiry or fencing, and cancellation. They carry no subject, body, or recipient.
    """
    revisions = list(
        MessageRevision.objects.filter(campaign=campaign).order_by("revision_number")[: MAX_TIMELINE_ROWS + 1]
    )
    intents = list(
        CommunicationIntent.objects.filter(campaign=campaign)
        .select_related("revision")
        .order_by("created_at", "pk")[: MAX_TIMELINE_ROWS + 1]
    )
    truncated = len(revisions) > MAX_TIMELINE_ROWS or len(intents) > MAX_TIMELINE_ROWS
    revisions, intents = revisions[:MAX_TIMELINE_ROWS], intents[:MAX_TIMELINE_ROWS]

    user_ids = {campaign.created_by_id, campaign.cancelled_by_user_id}
    user_ids |= {revision.actor_user_id for revision in revisions} | {intent.actor_user_id for intent in intents}
    labels = dict(get_user_model().objects.filter(pk__in=user_ids - {None}).values_list("pk", "username"))

    entries = [
        _entry(
            "created",
            campaign.created_at,
            _actor(
                campaign.created_by_id, campaign.actor_token_id, labels, principal_uuid=campaign.creator_principal_uuid
            ),
        )
    ]
    for revision in revisions:
        actor = _actor(
            revision.actor_user_id, revision.actor_token_id, labels, principal_uuid=revision.actor_principal_uuid
        )
        entries.append(_entry("revised", revision.created_at, actor, revision_number=revision.revision_number))
    for intent in intents:
        entries.extend(_intent_entries(intent, labels))
    if campaign.cancelled_at is not None:
        actor = _actor(
            campaign.cancelled_by_user_id,
            campaign.cancelled_by_token_id,
            labels,
            principal_uuid=campaign.cancelled_by_principal_uuid,
        )
        entries.append(_entry("cancelled", campaign.cancelled_at, actor))
    order = {id(item): index for index, item in enumerate(entries)}
    entries.sort(key=lambda item: (item["at"], order[id(item)]))
    return {"entries": entries, "truncated": truncated}


def preview(campaign_id: UUID, revision_id: UUID, *, actor: AdmissionActor) -> dict[str, Any]:
    """Read reviewed content only after current complete-target admission."""
    with authorized_campaign(campaign_id, actor) as campaign:
        return _preview(campaign, revision_id)


def delivery_report(campaign_id: UUID, *, actor: AdmissionActor, limit: int, offset: int) -> dict[str, Any]:
    """Read aggregate delivery facts behind the owning admission boundary."""
    with authorized_campaign(campaign_id, actor) as campaign:
        return _delivery_report(campaign, limit=limit, offset=offset)


def recipient_rows(
    campaign_id: UUID, *, actor: AdmissionActor, intent_id: UUID, event_id: UUID, limit: int, offset: int
) -> dict[str, Any]:
    """Read minimal roster facts with independent exact-event roster authority."""
    with authorized_campaign(campaign_id, actor, participant_event_id=event_id) as campaign:
        return _recipient_rows(campaign, intent_id=intent_id, event_id=event_id, limit=limit, offset=offset)


def activity_timeline(campaign_id: UUID, *, actor: AdmissionActor) -> dict[str, Any]:
    """Read body-free attribution only after complete-target admission."""
    with authorized_campaign(campaign_id, actor) as campaign:
        return _activity_timeline(campaign)


__all__ = [
    "MAX_TIMELINE_ROWS",
    "activity_timeline",
    "delivery_report",
    "list_target_options",
    "list_targets",
    "preview",
    "recipient_rows",
    "trigger_projection",
]
