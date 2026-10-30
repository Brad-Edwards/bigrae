"""Recipient fences the delivery worker applies before transport (ADR-051-R12, ADR-065-R4).

Split from ``delivery`` for the python:S104 file-size budget. Participant rows
recheck live viewing eligibility; organizer rows recheck the live candidate set
and, outside SQL locks, the pinned native-policy action. None of these reads the
encrypted delivery coordinate; only the email adapter decrypts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctf.enums_communication import RecipientKind
from ctf.models import RecipientSnapshot

if TYPE_CHECKING:
    from ctf.models import DeliveryAttempt


def recipient_still_eligible(snapshot: RecipientSnapshot) -> bool:
    """Return True only while the snapshot's participant is still viewing-eligible.

    A participant removed, deactivated, or otherwise made ineligible after the
    claim must not reach an adapter (ADR-051-R12). This rechecks the live
    event-scoped participant with the same predicate the audience resolver uses,
    so reclaimed and retried work re-validates lifecycle scope right before I/O.
    """
    from ctf.models import CTFParticipant
    from ctf.services.participant import viewing_participant_q

    if snapshot.participant_public_id is None:
        return False
    return CTFParticipant.objects.filter(
        viewing_participant_q(),
        id=snapshot.participant_public_id,
        event_id=snapshot.event_id,
    ).exists()


def organizer_still_candidate(snapshot: RecipientSnapshot) -> bool:
    """Return True only while the snapshot's principal is still a current owner or co-organizer.

    Staff revocation, an ownership change without retention, or an inactive account
    removes the candidate, so reclaimed and retried work never reaches a former
    organizer (ADR-065-R4). ``organizer_policy_stop`` rechecks native policy
    before the claim lock.
    """
    from ctf.services.event.staff import event_notification_organizers

    return snapshot.principal_uuid in event_notification_organizers(snapshot.event)


def organizer_policy_stop(attempt: DeliveryAttempt) -> str | None:
    """Recheck an organizer snapshot's pinned action against live native policy.

    Returns ``None`` for a participant row or an allowed organizer,
    ``"organizer_policy_denied"`` for an explicit denial, and
    ``"authority_unavailable"`` when the evaluator cannot decide; that work stays
    retriable and never reaches the transport.
    """
    from ctf.exceptions import CTFError
    from ctf.services.unified_authorization import principals_holding_event_action

    # Identity fields only: the encrypted coordinate is decrypted solely by the email adapter.
    snapshot = (
        RecipientSnapshot.objects.filter(pk=attempt.snapshot_id)
        .values("recipient_kind", "principal_uuid", "event_id", "intent__campaign__audience_spec")
        .first()
    )
    if snapshot is None or snapshot["recipient_kind"] != RecipientKind.ORGANIZER.value:
        return None
    principal_uuid = snapshot["principal_uuid"]
    action = (snapshot["intent__campaign__audience_spec"] or {}).get("action", "")
    try:
        allowed = (
            principals_holding_event_action(snapshot["event_id"], [principal_uuid], action)
            if principal_uuid is not None
            else frozenset()
        )
    except CTFError:
        return "authority_unavailable"
    return None if principal_uuid in allowed else "organizer_policy_denied"
