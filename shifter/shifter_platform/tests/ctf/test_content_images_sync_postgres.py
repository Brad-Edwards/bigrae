"""PostgreSQL proof that image syncs for one event run one at a time (core-F2, #2090).

Every content commit queues a sync after commit. Two syncs for one event can
overlap, and an earlier sync that read older content must not finish last.
SQLite cannot prove row-lock serialization, so this runs on the Postgres lane.
"""

from __future__ import annotations

import hashlib
import threading

import pytest
from django.db import connection

from ctf.models import CTFContentImage, CTFEventPage
from ctf.services import content_images as svc

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

_OLD = "https://img.example.test/old.png"
_NEW = "https://img.example.test/new.png"


def _sync_in_thread(event_id, name: str, errors: list[BaseException]) -> threading.Thread:
    def run() -> None:
        try:
            svc.sync_event_content_images(event_id)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


def test_an_earlier_sync_cannot_overwrite_a_later_one(ctf_event, monkeypatch):
    page = CTFEventPage.objects.create(event=ctf_event, title="Gallery", slug="gallery", body=f"![a]({_OLD})")
    svc.sync_event_content_images(ctf_event.id)
    snapshot_taken = threading.Event()
    resume = threading.Event()
    real_urls = svc.event_image_urls

    def urls_then_pause(event_id):
        urls = real_urls(event_id)
        if threading.current_thread().name == "first":
            snapshot_taken.set()
            resume.wait(timeout=10)
        return urls

    monkeypatch.setattr(svc, "event_image_urls", urls_then_pause)
    errors: list[BaseException] = []

    first = _sync_in_thread(ctf_event.id, "first", errors)
    assert snapshot_taken.wait(timeout=10)
    # The organizer commits new content while the first sync holds its older snapshot.
    CTFEventPage.objects.filter(pk=page.pk).update(body=f"![b]({_NEW})")
    second = _sync_in_thread(ctf_event.id, "second", errors)
    second.join(timeout=1)
    second_waited = second.is_alive()
    resume.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert errors == []
    assert second_waited, "the second sync must wait for the first sync's event lock"
    digests = set(CTFContentImage.objects.filter(event=ctf_event).values_list("source_digest", flat=True))
    assert digests == {hashlib.sha256(_NEW.encode()).hexdigest()}
