"""Current native admission for console reads, before any report data is exposed."""

from contextlib import contextmanager
from uuid import UUID

from django.db import transaction

from ctf.exceptions import CTFCommunicationError, CTFError, CTFPermissionError
from ctf.models import CommunicationCampaign, CTFEvent
from ctf.services.authorization import admit_event_actions, assert_event_action
from ctf.services.communication.admission import (
    AdmissionActor,
    prepare_admission,
    prepare_collection_actor,
    reauthorize,
)
from shared.api_tokens.scopes import CTF_COMMUNICATION_READ


@contextmanager
def authorized_campaign(
    campaign_id: UUID,
    actor: AdmissionActor,
    *,
    participant_event_id: UUID | None = None,
    required_scope: str = CTF_COMMUNICATION_READ,
):
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
    roster_admission = None
    if participant_event_id is not None:
        event = next((event for event in events if event.pk == participant_event_id), None)
        if event is None:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
        try:
            roster_admission = admit_event_actions(
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
    with transaction.atomic():
        locked_events = list(
            CTFEvent.objects.select_for_update()
            .filter(communication_target_links__campaign_id=campaign.pk)
            .order_by("pk")
        )
        locked = CommunicationCampaign.objects.select_for_update().get(pk=campaign.pk)
        reauthorize(locked, locked_events, actor, required_scope=required_scope, audit_authority=False)
        if roster_admission is not None:
            event = next((event for event in locked_events if event.pk == participant_event_id), None)
            if event is None:
                raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
            try:
                assert_event_action(roster_admission, event, "event.manage_participants")
            except CTFPermissionError:
                raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND") from None
        yield locked
