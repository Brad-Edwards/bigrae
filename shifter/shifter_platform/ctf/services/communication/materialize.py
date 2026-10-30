"""Recipient snapshots, receipts and delivery commands written at release (ADR-051, ADR-065-R4).

Split from ``release`` for the python:S104 file-size budget. The caller holds the
release transaction and the target-event locks; nothing here calls a transport.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from ctf.enums_communication import CommunicationChannel, DeliveryStatus, RecipientKind
from ctf.models import (
    CommunicationCampaign,
    CommunicationIntent,
    CTFEvent,
    CTFParticipant,
    DeliveryAttempt,
    RecipientReceipt,
    RecipientSnapshot,
)
from ctf.services.communication.audience import OrganizerRecipient, resolve_organizer_recipients, resolve_recipients

Recipient = CTFParticipant | OrganizerRecipient


def _snapshot(
    intent: CommunicationIntent, recipient: Recipient, event_id: UUID | None
) -> tuple[RecipientSnapshot, UUID]:
    """Create the immutable snapshot for one recipient and return its stable identity."""
    if isinstance(recipient, OrganizerRecipient):
        if event_id is None:
            raise ValueError("Organizer snapshots need their qualifying event")
        snapshot = RecipientSnapshot.objects.create(
            intent=intent,
            event_id=event_id,
            recipient_kind=RecipientKind.ORGANIZER.value,
            principal_uuid=recipient.principal_uuid,
            user_id=recipient.user_id,
            delivery_coordinate=recipient.email,
        )
        return snapshot, recipient.principal_uuid
    snapshot = RecipientSnapshot.objects.create(
        intent=intent,
        event_id=recipient.event_id,
        participant=recipient,
        participant_public_id=recipient.id,
        team_id=recipient.team_id,
        user_id=recipient.user_id,
        delivery_coordinate=recipient.email or "",
    )
    return snapshot, recipient.id


def materialize_recipients(
    intent: CommunicationIntent,
    recipients: list[CTFParticipant] | list[OrganizerRecipient],
    channels: list[str],
    now: datetime,
    *,
    organizer_event_id: UUID | None = None,
) -> None:
    """Write the per-recipient snapshots, receipts, and per-channel delivery commands.

    In-app availability is committed here, at admission: when ``in_app`` is a
    selected channel the ``RecipientReceipt`` (the durable inbox entry) is created
    in this transaction, so availability never depends on a worker later processing
    the queued wake-up command (#2098). When ``in_app`` is not selected no receipt is
    created, so an email-only campaign never exposes an in-app inbox item.
    """
    in_app_selected = CommunicationChannel.IN_APP.value in channels
    for recipient in recipients:
        snapshot, identity = _snapshot(intent, recipient, organizer_event_id)
        if in_app_selected:
            RecipientReceipt.objects.create(snapshot=snapshot)
        DeliveryAttempt.objects.bulk_create(
            [
                DeliveryAttempt(
                    intent=intent,
                    snapshot=snapshot,
                    channel=channel,
                    status=DeliveryStatus.QUEUED.value,
                    idempotency_key=f"{intent.idempotency_key}:{identity}:{channel}",
                    due_at=intent.due_at or now,
                )
                for channel in channels
            ]
        )


def resolve_audience(
    campaign: CommunicationCampaign,
    target_events: list[CTFEvent],
    target_event_ids: list[UUID],
    organizers: frozenset[UUID] | None,
) -> tuple[list[CTFParticipant] | list[OrganizerRecipient], UUID | None]:
    """Resolve recipients under the locks; organizers intersect the pre-lock policy decision."""
    if organizers is None:
        return resolve_recipients(set(target_event_ids), campaign.audience_spec), None
    event = target_events[0]
    return resolve_organizer_recipients(event, organizers), event.pk
