"""Current native admission for console reads, before any report data is exposed."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from ctf.exceptions import CTFCommunicationError, CTFError, CTFPermissionError
from ctf.models import CommunicationCampaign, CTFEvent
from ctf.services.actors import EventActionAdmission
from ctf.services.authorization import admit_event_actions, assert_event_action
from ctf.services.communication.admission import (
    AdmissionActor,
    prepare_admission,
    prepare_collection_actor,
    reauthorize,
)
from shared.api_tokens.scopes import CTF_COMMUNICATION_READ

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def authorized_campaign(
    campaign_id: UUID,
    actor: AdmissionActor,
    *,
    participant_event_id: UUID | None = None,
    required_scope: str = CTF_COMMUNICATION_READ,
) -> Iterator[CommunicationCampaign]:
    """Preflight complete targets and optional roster access, then fence exact rows.

    Reports follow Event → Campaign lock order. Remote policy is evaluated only
    before SQL locks; an extra roster action never substitutes for authoring
    authority on every target event.
    """
    actor = prepare_collection_actor(actor, required_scope=required_scope)
    if actor.credential is None or actor.system:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_ACTOR_DENIED")
    campaign = CommunicationCampaign.objects.filter(pk=campaign_id).first()
    if campaign is None:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
    events = list(campaign.target_events.order_by("pk"))
    actor = prepare_admission(campaign, events, actor, required_scope=required_scope)
    roster_admission = _admit_roster(actor, events, participant_event_id)
    with transaction.atomic():
        locked_events = list(
            CTFEvent.objects.select_for_update()
            .filter(communication_target_links__campaign_id=campaign.pk)
            .order_by("pk")
        )
        locked = CommunicationCampaign.objects.select_for_update().get(pk=campaign.pk)
        reauthorize(locked, locked_events, actor, required_scope=required_scope, audit_authority=False)
        _fence_roster(roster_admission, locked_events, participant_event_id)
        yield locked


def _roster_event(events: list[CTFEvent], event_id: UUID | None) -> CTFEvent:
    """Resolve roster access only within the exact campaign target set."""
    event = next((event for event in events if event.pk == event_id), None)
    if event is None:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
    return event


def _admit_roster(actor: AdmissionActor, events: list[CTFEvent], event_id: UUID | None) -> EventActionAdmission | None:
    """Admit optional roster policy before acquiring event or campaign locks."""
    if event_id is None:
        return None
    event = _roster_event(events, event_id)
    try:
        return admit_event_actions(
            actor.compatibility_user_id if actor.compatibility_user_id is not None else actor.credential,
            event,
            "event.manage_participants",
        )
    except CTFPermissionError:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND") from None
    except CTFError:
        raise CTFCommunicationError(
            "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        ) from None


def _fence_roster(admission: EventActionAdmission | None, events: list[CTFEvent], event_id: UUID | None) -> None:
    """Fence the admitted roster under the same locks as campaign read admission."""
    if admission is not None:
        event = _roster_event(events, event_id)
        try:
            assert_event_action(admission, event, "event.manage_participants")
        except CTFPermissionError:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND") from None
