"""Retained CTF membership invalidation and mandatory parked selector boundary."""

from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ctf.enums import ParticipantStatus
from ctf.models import CTFParticipant, CTFTeam
from ctf.services import resolve_model_access_selector
from shared.model_access import SelectorKind, SharingSelector
from shared.model_access.availability import ModelAccessParked

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "kind", [SelectorKind.CTF_EVENT, SelectorKind.CTF_TEAM, SelectorKind.CTF_COHORT, SelectorKind.SELECTED_RANGES]
)
def test_public_ctf_selector_is_parked_before_event_data(kind, organizer_user):
    selector = SharingSelector(kind=kind, ids=(str(uuid4()),))
    with CaptureQueriesContext(connection) as queries, pytest.raises(ModelAccessParked):
        resolve_model_access_selector(organizer_user, selector)
    assert len(queries) == 0


def test_participant_membership_mutation_invalidates_old_and_new_owner_refs(
    ctf_event_team,
    monkeypatch,
):
    old_team = CTFTeam.objects.create(event=ctf_event_team, name="Old")
    new_team = CTFTeam.objects.create(event=ctf_event_team, name="New")
    participant = CTFParticipant.objects.create(
        event=ctf_event_team,
        name="Mover",
        email="mover@example.test",
        status=ParticipantStatus.REGISTERED.value,
        registered_at=timezone.now(),
        team=old_team,
    )
    commands = []
    monkeypatch.setattr(
        "ctf.bridges.cms_invalidate_model_access_authority",
        lambda command: commands.append(command) or 1,
    )

    participant.team = new_team
    participant.save(update_fields=["team", "updated_at"])

    references = {(item.owner, item.reference) for command in commands for item in command.authority_refs}
    assert ("ctf", f"event:{ctf_event_team.pk}") in references
    assert ("ctf", f"team:{old_team.pk}") in references
    assert ("ctf", f"team:{new_team.pk}") in references
    assert ("ctf", f"participant:{participant.pk}") in references
