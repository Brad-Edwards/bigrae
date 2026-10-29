"""Participants see only images from content they can currently read (#2090).

A copy must not reveal an unreleased challenge, a hidden solution, or an unsent
announcement before the participant could read that content itself.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from ctf.enums import ChallengeCategory, ChallengeDifficulty, NotificationStatus, NotificationType
from ctf.models import ContentImageStatus, CTFChallenge, CTFContentImage, CTFEventPage, CTFNotification
from ctf.services import content_images as svc


def _challenge(event, **overrides):
    defaults = {
        "event": event,
        "name": f"c-{overrides.get('description', '')[-8:]}",
        "category": ChallengeCategory.WEB.value,
        "points": 100,
        "difficulty": ChallengeDifficulty.EASY.value,
    }
    defaults.update(overrides)
    return CTFChallenge.objects.create(**defaults)


@pytest.fixture
def content(ctf_event, ctf_participant):
    ctf_event.logo_url = "https://img.example.test/logo.png"
    ctf_event.rules = "![r](https://img.example.test/rules.png)"
    ctf_event.save()
    _challenge(
        ctf_event,
        description="![open](https://img.example.test/open.png)",
        solution="![sol](https://img.example.test/sol.png)",
    )
    _challenge(
        ctf_event,
        description="![later](https://img.example.test/later.png)",
        release_time=timezone.now() + timedelta(days=1),
    )
    _challenge(ctf_event, description="![hidden](https://img.example.test/hidden.png)", visibility="hidden")
    CTFEventPage.objects.create(event=ctf_event, title="Info", slug="info", body="![p](https://img.example.test/p.png)")
    for status, url in ((NotificationStatus.SENT, "sent"), (NotificationStatus.DRAFT, "draft")):
        CTFNotification.objects.create(
            event=ctf_event,
            notification_type=NotificationType.ANNOUNCEMENT.value,
            status=status.value,
            created_by=ctf_event.created_by,
            subject=url,
            body=f"![a](https://img.example.test/{url}.png)",
        )
    svc.sync_event_content_images(ctf_event.id)
    CTFContentImage.objects.filter(event=ctf_event).update(
        status=ContentImageStatus.READY, storage_key="k", size_bytes=1, sha256="x", content_type="image/png"
    )
    return ctf_event


@pytest.mark.django_db
def test_participant_sees_only_currently_readable_content(content, ctf_participant):
    visible = svc.participant_visible_image_urls(ctf_participant)
    assert visible == {
        "https://img.example.test/logo.png",
        "https://img.example.test/rules.png",
        "https://img.example.test/open.png",
        "https://img.example.test/p.png",
        "https://img.example.test/sent.png",
    }
    assert set(svc.participant_image_map(ctf_participant)) == visible


@pytest.mark.django_db
def test_solution_images_appear_after_the_event_ends(content, ctf_participant):
    content.status = "ended"
    content.save(update_fields=["status", "updated_at"])
    ctf_participant.refresh_from_db()
    assert "https://img.example.test/sol.png" in svc.participant_visible_image_urls(ctf_participant)


@pytest.mark.django_db
def test_image_read_authority(
    content, ctf_participant, organizer_user, second_participant_user, live_session_credential
):
    from ctf.models import CTFParticipant

    hidden = CTFContentImage.objects.get(event=content, source_url="https://img.example.test/hidden.png")
    logo = CTFContentImage.objects.get(event=content, source_url="https://img.example.test/logo.png")

    assert svc.can_read_content_image(live_session_credential(ctf_participant.user), logo)
    assert not svc.can_read_content_image(live_session_credential(ctf_participant.user), hidden)
    # Event staff (the owner) read every image of the event.
    assert svc.can_read_content_image(live_session_credential(organizer_user), hidden)
    # A user with no participation in this event reads nothing.
    assert not CTFParticipant.objects.filter(user=second_participant_user, event=content).exists()
    assert not svc.can_read_content_image(live_session_credential(second_participant_user), logo)
