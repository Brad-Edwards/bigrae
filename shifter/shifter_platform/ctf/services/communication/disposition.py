"""Persisted occurrence delivery state for scoped communications (ADR-071, #1525).

``CommunicationIntent.delivery_state`` is the aggregate delivery outcome of one
occurrence, kept separate from its release lifecycle (``IntentStatus``). This
module is its only writer. Every writer locks the intent row, derives the state
from committed per-recipient rows, and writes it only when it changes, so
concurrent final settlements converge on the last committed truth.

Lock order: callers may already hold event/campaign/attempt locks (release and
lifecycle); this module then locks intent rows in a stable order and only reads
attempts and receipts. The worker calls it after its settle transaction commits,
holding no attempt lock. No path locks an intent and then an attempt.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Exists, OuterRef
from django.utils import timezone

from ctf.enums_communication import CommunicationChannel, DeliveryStatus, IntentDeliveryState
from ctf.models import CommunicationIntent, DeliveryAttempt, ParticipantReceipt, RecipientSnapshot

_IN_APP = CommunicationChannel.IN_APP.value
_OUTSTANDING = (DeliveryStatus.QUEUED.value, DeliveryStatus.CLAIMED.value, DeliveryStatus.RETRY_DUE.value)
_BUCKETS = {
    DeliveryStatus.QUEUED.value: "pending",
    DeliveryStatus.CLAIMED.value: "pending",
    DeliveryStatus.RETRY_DUE.value: "pending",
    DeliveryStatus.ACCEPTED.value: "accepted",
    DeliveryStatus.PERMANENT_FAILURE.value: "failed",
    DeliveryStatus.EXPIRED.value: "failed",
    DeliveryStatus.CANCELLED.value: "withdrawn",
    DeliveryStatus.SUPPRESSED.value: "withdrawn",
}


def derive_delivery_state(intent: CommunicationIntent) -> str:
    """Return the aggregate state from committed rows (no write).

    In-app success is the committed inbox receipt, never the wake-up command. A
    transport channel counts its ``DeliveryAttempt`` statuses; a recipient with no
    command for a selected channel counts as failed.
    """
    if intent.released_at is None:
        return IntentDeliveryState.NOT_RELEASED.value
    recipients = RecipientSnapshot.objects.filter(intent_id=intent.pk).count()
    if recipients == 0:
        return IntentDeliveryState.EMPTY.value
    tally: Counter[str] = Counter()
    for channel in intent.channels:
        if channel == _IN_APP:
            available = ParticipantReceipt.objects.filter(snapshot__intent_id=intent.pk).count()
            tally["accepted"] += available
            tally["failed"] += recipients - available
            continue
        counted = 0
        rows = (
            DeliveryAttempt.objects.filter(intent_id=intent.pk, channel=channel)
            .values_list("status")
            .annotate(total=Count("id"))
        )
        for status, total in rows:
            tally[_BUCKETS[status]] += total
            counted += total
        tally["failed"] += max(recipients - counted, 0)
    return _state_from(tally)


def _state_from(tally: Counter[str]) -> str:
    """Reduce per-recipient channel outcomes to one closed state (first rule wins)."""
    rules = [
        (tally["pending"] > 0, IntentDeliveryState.PENDING),
        (tally["accepted"] == sum(tally.values()), IntentDeliveryState.ACCEPTED),
        (tally["accepted"] > 0, IntentDeliveryState.PARTIAL),
        (tally["failed"] > 0, IntentDeliveryState.FAILED),
    ]
    return next((state for matched, state in rules if matched), IntentDeliveryState.WITHDRAWN).value


def refresh_delivery_state(intent_ids: Iterable[UUID]) -> None:
    """Lock each intent, derive its state, and persist a change with its time."""
    for intent_id in sorted(set(intent_ids), key=str):
        with transaction.atomic():
            intent = (
                CommunicationIntent.objects.select_for_update()
                .only("id", "released_at", "channels", "delivery_state")
                .filter(pk=intent_id)
                .first()
            )
            if intent is None:
                continue
            state = derive_delivery_state(intent)
            if state != intent.delivery_state:
                CommunicationIntent.objects.filter(pk=intent_id).update(
                    delivery_state=state, delivery_state_changed_at=timezone.now()
                )


def refresh_after_settlement(intent_id: UUID, channel: str) -> None:
    """Worker path: recount an occurrence only when its last transport command settles.

    In-app success is fixed at release by the committed receipt, so an in-app
    wake-up never changes the state. While another transport command is still
    outstanding the state stays ``pending``, so an indexed existence check avoids
    a full recount per settlement. Each settlement commits before this check, so
    the last settling worker always observes no outstanding command and refreshes.
    """
    if channel == _IN_APP:
        return
    outstanding = DeliveryAttempt.objects.filter(intent_id=intent_id, status__in=_OUTSTANDING).exclude(channel=_IN_APP)
    if outstanding.exists():
        return
    refresh_delivery_state([intent_id])


def reconcile_delivery_states(*, limit: int) -> int:
    """Refresh pending occurrences that have no outstanding transport command.

    Recovers a worker that settled a command but stopped before its refresh.
    Bounded by ``limit``; returns the number of occurrences refreshed.
    """
    outstanding = DeliveryAttempt.objects.filter(intent_id=OuterRef("pk"), status__in=_OUTSTANDING).exclude(
        channel=_IN_APP
    )
    stale = list(
        CommunicationIntent.objects.filter(delivery_state=IntentDeliveryState.PENDING.value)
        .exclude(Exists(outstanding))
        .values_list("id", flat=True)[:limit]
    )
    refresh_delivery_state(stale)
    return len(stale)
