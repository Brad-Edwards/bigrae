"""Saving organizer content syncs its image rows after commit (#2090)."""

from __future__ import annotations

import pytest

from ctf.enums import ChallengeCategory, ChallengeDifficulty, NotificationType
from ctf.models import CTFChallenge, CTFContentImage, CTFEventPage, CTFNotification


def _urls(event):
    return set(CTFContentImage.objects.filter(event=event).values_list("source_url", flat=True))


@pytest.mark.django_db
def test_each_content_source_syncs_on_commit(ctf_event, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        ctf_event.logo_url = "https://img.example.test/logo.png"
        ctf_event.save(update_fields=["logo_url", "updated_at"])
    assert _urls(ctf_event) == {"https://img.example.test/logo.png"}

    with django_capture_on_commit_callbacks(execute=True):
        challenge = CTFChallenge.objects.create(
            event=ctf_event,
            name="Pictures",
            description="![d](https://img.example.test/desc.png)",
            category=ChallengeCategory.WEB.value,
            points=100,
            difficulty=ChallengeDifficulty.EASY.value,
        )
        CTFEventPage.objects.create(
            event=ctf_event, title="Info", slug="info", body="![p](https://img.example.test/p.png)"
        )
        CTFNotification.objects.create(
            event=ctf_event,
            notification_type=NotificationType.ANNOUNCEMENT.value,
            created_by=ctf_event.created_by,
            subject="Hi",
            body="![a](https://img.example.test/a.png)",
        )
    assert _urls(ctf_event) == {
        "https://img.example.test/logo.png",
        "https://img.example.test/desc.png",
        "https://img.example.test/p.png",
        "https://img.example.test/a.png",
    }

    with django_capture_on_commit_callbacks(execute=True):
        challenge.delete()  # soft delete is a save
    assert "https://img.example.test/desc.png" not in _urls(ctf_event)


@pytest.mark.django_db
def test_saves_that_do_not_touch_content_do_not_sync(ctf_event, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        ctf_event.name = "Renamed"
        ctf_event.save(update_fields=["name", "updated_at"])
    assert callbacks == []
