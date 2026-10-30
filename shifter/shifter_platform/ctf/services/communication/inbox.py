"""Live parent-scoped inbox policy and explicit recipient receipt transitions.

Participants read participant rows through their live participation. Organizers
(the event owner and live co-organizers) read organizer rows through live native
policy for each alert's pinned action (ADR-065-R4). Neither path reaches the
other's rows.
"""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from django.contrib.auth.models import AnonymousUser, User
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from ctf.communication_contracts import ORGANIZER_ALERT_ACTIONS
from ctf.enums_communication import AcknowledgementPolicy, InboxFilter, ReceiptAction, RecipientKind
from ctf.exceptions import CTFCommunicationError, CTFPermissionError
from ctf.models import CTFEvent, CTFParticipant, RecipientReceipt, RecipientSnapshot
from ctf.services.communication.retention import expired_campaign_ids
from ctf.services.participant import viewing_participant_q
from ctf.services.participant.accounts import live_participant_for_user
from management.services import is_ctf_password_change_required, is_temporary_ctf_account
from shared.credentials import CredentialContext

# The pinned policy's unmet obligation: a read for READ, an acknowledgement for EXPLICIT.
_OPEN_OBLIGATION = Q(
    intent__acknowledgement_policy=AcknowledgementPolicy.READ.value, receipt__read_at__isnull=True
) | Q(intent__acknowledgement_policy=AcknowledgementPolicy.EXPLICIT.value, receipt__acknowledged_at__isnull=True)
_DISMISSED = Q(receipt__dismissed_at__isnull=False)
_FILTERS = {
    InboxFilter.INBOX: ~_DISMISSED | _OPEN_OBLIGATION,
    InboxFilter.ACTION_REQUIRED: _OPEN_OBLIGATION,
    InboxFilter.DISMISSED: _DISMISSED,
}


def _deny() -> NoReturn:
    """Reject unavailable participation with an opaque domain error."""

    raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_PARTICIPANT_DENIED")


def has_open_obligation(snapshot: RecipientSnapshot) -> bool:
    """Return whether the snapshot's pinned policy still needs a participant action."""
    receipt = snapshot.receipt
    policy = snapshot.intent.acknowledgement_policy
    if policy == AcknowledgementPolicy.READ.value:
        return receipt.read_at is None
    if policy == AcknowledgementPolicy.EXPLICIT.value:
        return receipt.acknowledged_at is None
    return False


def participant_for_inbox(user: User | AnonymousUser, event_id: UUID) -> CTFParticipant:
    """Receipt interaction grants no play authority; disqualified readers remain eligible."""
    if user is None or not user.is_active or getattr(getattr(user, "profile", None), "deleted_at", None):
        _deny()
    if is_temporary_ctf_account(user):
        live = live_participant_for_user(user)
        if live is None or live.event_id != event_id or is_ctf_password_change_required(user):
            _deny()
    participant = CTFParticipant.objects.filter(
        viewing_participant_q(), user_id=user.pk, event_id=event_id, event__deleted_at__isnull=True
    ).first()
    if participant is None:
        _deny()
    return participant


def inbox_for_participant(user: User | AnonymousUser, event_id: UUID) -> QuerySet[RecipientSnapshot]:
    """Only committed, retained in-app availability for this exact participation is visible."""
    participant = participant_for_inbox(user, event_id)
    return (
        RecipientSnapshot.objects.filter(
            event_id=event_id,
            recipient_kind=RecipientKind.PARTICIPANT.value,
            participant_public_id=participant.pk,
            user_id=user.pk,
            receipt__isnull=False,
            intent__released_at__isnull=False,
        )
        .exclude(intent__campaign_id__in=expired_campaign_ids())
        .select_related("intent__revision", "receipt")
        .order_by("-intent__released_at", "-pk")
    )


def select_inbox_view(inbox: QuerySet[RecipientSnapshot], view: InboxFilter) -> QuerySet[RecipientSnapshot]:
    """Narrow an already authorized inbox to one closed selection."""
    return inbox.filter(_FILTERS[view])


def inbox_counts(inbox: QuerySet[RecipientSnapshot]) -> dict[str, int]:
    """Count unread default-view messages and open obligations in one query."""
    return inbox.aggregate(
        unread_count=Count("pk", filter=_FILTERS[InboxFilter.INBOX] & Q(receipt__read_at__isnull=True)),
        action_required_count=Count("pk", filter=_OPEN_OBLIGATION),
    )


def interact_with_receipt(
    user: User | AnonymousUser, event_id: UUID, snapshot_id: UUID, action: ReceiptAction
) -> RecipientSnapshot:
    """Serialize live admission with event/participant removal and keep timestamps stable."""
    with transaction.atomic():
        if not CTFEvent.objects.select_for_update().filter(pk=event_id).exists():
            _deny()
        participant = participant_for_inbox(user, event_id)
        CTFParticipant.objects.select_for_update().get(pk=participant.pk)
        snapshot = inbox_for_participant(user, event_id).filter(pk=snapshot_id).first()
        if snapshot is None:
            _deny()
        return _record_receipt_action(snapshot, action)


def _record_receipt_action(snapshot: RecipientSnapshot, action: ReceiptAction) -> RecipientSnapshot:
    """Set one first-write receipt timestamp under the receipt row lock."""
    receipt = RecipientReceipt.objects.select_for_update().get(snapshot=snapshot)
    if (
        action is ReceiptAction.ACKNOWLEDGE
        and snapshot.intent.acknowledgement_policy != AcknowledgementPolicy.EXPLICIT.value
    ):
        raise CTFCommunicationError("Acknowledgement not required", code="CTF_COMMUNICATION_ACK_POLICY")
    field = action.receipt_field
    if getattr(receipt, field) is None:
        setattr(receipt, field, timezone.now())
        receipt.save(update_fields=[field, "updated_at"])
    snapshot.receipt = receipt
    return snapshot


def _deny_organizer() -> NoReturn:
    """Reject an unavailable organizer reader with an opaque domain error."""

    raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_RECIPIENT_DENIED")


def _organizer_event(credential: CredentialContext, event_id: UUID) -> CTFEvent:
    """Return the live event when the session principal is a current owner or co-organizer."""
    from ctf.services.event.staff import event_notification_organizers

    if credential.kind != "session":
        _deny_organizer()
    event = CTFEvent.objects.filter(pk=event_id, deleted_at__isnull=True).first()
    if event is None or credential.principal.uuid not in event_notification_organizers(event):
        _deny_organizer()
    return event


def _allowed_alert_actions(credential: CredentialContext, event: CTFEvent) -> list[str]:
    """Check live native policy for each closed alert action; unavailability propagates."""
    from ctf.services.authorization import assert_event_action

    allowed = []
    for action in sorted(ORGANIZER_ALERT_ACTIONS):
        try:
            assert_event_action(credential, event, action)
        except CTFPermissionError:
            continue
        allowed.append(action)
    return allowed


def organizer_inbox(credential: CredentialContext, event_id: UUID) -> QuerySet[RecipientSnapshot]:
    """Only a current organizer sees released organizer rows whose pinned action policy allows.

    Remote policy runs here, before any SQL lock. A participant row, another
    principal's row, or an alert whose action the reader no longer holds is absent.
    """
    event = _organizer_event(credential, event_id)
    allowed = _allowed_alert_actions(credential, event)
    if not allowed:
        _deny_organizer()
    return (
        RecipientSnapshot.objects.filter(
            event_id=event.pk,
            recipient_kind=RecipientKind.ORGANIZER.value,
            principal_uuid=credential.principal.uuid,
            receipt__isnull=False,
            intent__released_at__isnull=False,
            intent__campaign__audience_spec__action__in=allowed,
        )
        .exclude(intent__campaign_id__in=expired_campaign_ids())
        .select_related("intent__revision", "receipt")
        .order_by("-intent__released_at", "-pk")
    )


def interact_with_organizer_receipt(
    credential: CredentialContext, event_id: UUID, snapshot_id: UUID, action: ReceiptAction
) -> RecipientSnapshot:
    """Record an organizer read or dismiss; organizer alerts never need acknowledgement."""
    from ctf.services.event.staff import event_notification_organizers

    if action is ReceiptAction.ACKNOWLEDGE:
        raise CTFCommunicationError("Acknowledgement not required", code="CTF_COMMUNICATION_ACK_POLICY")
    visible = organizer_inbox(credential, event_id).filter(pk=snapshot_id)
    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().filter(pk=event_id, deleted_at__isnull=True).first()
        if event is None or credential.principal.uuid not in event_notification_organizers(event):
            _deny_organizer()
        snapshot = visible.first()
        if snapshot is None:
            _deny_organizer()
        return _record_receipt_action(snapshot, action)
