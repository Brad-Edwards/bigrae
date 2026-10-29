"""The dev/test-only CTF inbox seed for the browser lane (#2053)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework.test import APIClient

from ctf.management.commands.seed_e2e_ctf import (
    HOSTILE_SUBJECT,
    INBOX_PARTICIPANT_EMAIL,
    SAFE_SUBJECT,
)
from ctf.models import CommunicationIntent, CTFEvent

User = get_user_model()

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("personal_token_use_grant")]


def test_seed_publishes_an_idempotent_inbox_for_the_participant_actor():
    call_command("seed_e2e_ctf")
    call_command("seed_e2e_ctf")

    assert CTFEvent.objects.count() == 1
    assert CommunicationIntent.objects.count() == 2
    client = APIClient()
    client.force_login(User.objects.get(username=INBOX_PARTICIPANT_EMAIL))
    current = client.get("/api/v1/ctf/me/event/")
    assert current.status_code == 200, current.content
    inbox = client.get(f"/api/v1/ctf/me/events/{current.json()['event']['id']}/communications/").json()
    by_subject = {item["subject"]: item for item in inbox["results"]}
    assert set(by_subject) == {SAFE_SUBJECT, HOSTILE_SUBJECT}
    assert by_subject[SAFE_SUBJECT]["acknowledgement_policy"] == "explicit"
    # The hostile row is stored as-is; only the renderer and CSP may neutralize it.
    assert "<script>" in by_subject[HOSTILE_SUBJECT]["body"]


@override_settings(DEBUG=False, ENVIRONMENT="production")
def test_seed_refuses_production_posture():
    with pytest.raises(CommandError):
        call_command("seed_e2e_ctf")
    assert not CTFEvent.objects.exists()
