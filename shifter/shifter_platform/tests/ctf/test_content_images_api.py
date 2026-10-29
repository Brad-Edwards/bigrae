"""Image map endpoints and the first-party image view (#2090, ADR-036)."""

from __future__ import annotations

import hashlib

import pytest
from django.test import Client
from django.utils import timezone

from ctf.enums import ChallengeCategory, ChallengeDifficulty
from ctf.models import ContentImageStatus, CTFChallenge, CTFContentImage, CTFParticipant
from ctf.services import content_images as svc
from tests.ctf._api_flow_helpers import call_json

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24


class _Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def read_object_header(self, bucket, key, max_bytes, *, expected_identity=None):
        return self.objects[key][:max_bytes]


@pytest.fixture
def storage(monkeypatch):
    fake = _Storage()
    monkeypatch.setattr(svc, "get_object_storage", lambda: fake)
    return fake


@pytest.fixture
def images(ctf_event_active, storage):
    event = ctf_event_active
    event.logo_url = "https://img.example.test/logo.png"
    event.save()
    CTFChallenge.objects.create(
        event=event,
        name="Later",
        description="![later](https://img.example.test/later.png)",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
        visibility="hidden",
    )
    svc.sync_event_content_images(event.id)
    for row in CTFContentImage.objects.filter(event=event):
        key = f"ctf-content-images/{event.id}/{row.id}"
        storage.objects[key] = PNG
        CTFContentImage.objects.filter(pk=row.pk).update(
            status=ContentImageStatus.READY,
            storage_key=key,
            content_type="image/png",
            size_bytes=len(PNG),
            sha256=hashlib.sha256(PNG).hexdigest(),
        )
    return {row.source_url: row for row in CTFContentImage.objects.filter(event=event)}


@pytest.fixture
def participant_client(ctf_event_active, participant_user):
    from management.services import set_active_ctf_event

    CTFParticipant.objects.create(
        event=ctf_event_active,
        user=participant_user,
        email=participant_user.email,
        name="Viewer",
        status="active",
        registered_at=timezone.now(),
    )
    set_active_ctf_event(participant_user, ctf_event_active.pk)
    client = Client()
    client.force_login(participant_user)
    return client


def _get_image(client, image):
    return client.get(svc.image_url(image.id))


def test_participant_map_lists_only_readable_ready_images(images, participant_client):
    resp = call_json(participant_client, "get", "api_me_content_images")
    assert resp.status_code == 200
    logo = images["https://img.example.test/logo.png"]
    assert resp.json() == {"images": [{"source_url": logo.source_url, "url": svc.image_url(logo.id)}], "pending": 0}


def test_participant_map_counts_pending_visible_images_without_listing_them(images, participant_client):
    """The SPA polls while copies are pending (core-F5, #2090)."""
    logo = images["https://img.example.test/logo.png"]
    CTFContentImage.objects.filter(pk=logo.pk).update(status=ContentImageStatus.PENDING)
    # A pending image in hidden content is not counted either.
    hidden = images["https://img.example.test/later.png"]
    CTFContentImage.objects.filter(pk=hidden.pk).update(status=ContentImageStatus.PENDING)

    resp = call_json(participant_client, "get", "api_me_content_images")

    assert resp.json() == {"images": [], "pending": 1}


def test_organizer_map_lists_every_image_with_its_state(images, ctf_event_active, authenticated_organizer_client):
    later = images["https://img.example.test/later.png"]
    CTFContentImage.objects.filter(pk=later.pk).update(status=ContentImageStatus.FAILED, failure_code="too_large")

    resp = call_json(
        authenticated_organizer_client, "get", "api_event_content_images", kwargs={"event_id": ctf_event_active.id}
    )

    assert resp.status_code == 200
    rows = {row["source_url"]: row for row in resp.json()["images"]}
    assert rows["https://img.example.test/later.png"] == {
        "source_url": "https://img.example.test/later.png",
        "status": "failed",
        "failure_code": "too_large",
        "url": None,
    }
    logo = images["https://img.example.test/logo.png"]
    assert rows[logo.source_url]["url"] == svc.image_url(logo.id)


def test_organizer_map_refuses_non_staff(images, ctf_event_active, participant_client):
    resp = call_json(participant_client, "get", "api_event_content_images", kwargs={"event_id": ctf_event_active.id})
    assert resp.status_code == 403


def test_image_view_serves_verified_bytes_from_the_portal_origin(images, participant_client):
    resp = _get_image(participant_client, images["https://img.example.test/logo.png"])

    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp["Content-Type"] == "image/png"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["Cache-Control"] == "private, no-store"
    assert resp["Content-Disposition"] == "inline"


def test_image_view_hides_images_the_participant_cannot_read(images, participant_client, client):
    later = images["https://img.example.test/later.png"]
    assert _get_image(participant_client, later).status_code == 404
    # Anonymous requests are refused before any lookup.
    assert _get_image(client, later).status_code in (401, 403)


def test_image_view_serves_staff_and_refuses_unverified_copies(images, storage, authenticated_organizer_client):
    later = images["https://img.example.test/later.png"]
    assert _get_image(authenticated_organizer_client, later).status_code == 200

    storage.objects[later.storage_key] = PNG[:-1] + b"\x02"
    assert _get_image(authenticated_organizer_client, later).status_code == 404

    CTFContentImage.objects.filter(pk=later.pk).update(status=ContentImageStatus.PENDING)
    assert _get_image(authenticated_organizer_client, later).status_code == 404


def test_organizer_challenge_detail_names_its_event(ctf_event_active, authenticated_organizer_client):
    challenge = CTFChallenge.objects.create(
        event=ctf_event_active,
        name="Mapped",
        description="Find it.",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
    )
    resp = call_json(
        authenticated_organizer_client, "get", "api_challenge_detail", kwargs={"challenge_id": challenge.id}
    )
    assert resp.status_code == 200
    assert resp.json()["event_id"] == str(ctf_event_active.id)


@pytest.mark.parametrize("role", ["moderator", "judge"])
def test_bounded_staff_roles_cannot_list_or_download_images(
    images, ctf_event_active, second_organizer_user, organizer_credential, role
):
    """The collection includes hidden challenges and solutions (security-F1, #2090)."""
    from ctf.services.event import assign_event_staff

    assign_event_staff(ctf_event_active.pk, organizer_credential, second_organizer_user.identity_principal.uuid, role)
    client = Client()
    client.force_login(second_organizer_user)
    listing = call_json(client, "get", "api_event_content_images", kwargs={"event_id": ctf_event_active.id})
    assert listing.status_code == 403
    hidden = images["https://img.example.test/later.png"]
    assert _get_image(client, hidden).status_code == 404


def test_co_organizer_reads_the_whole_collection(images, ctf_event_active, second_organizer_user, organizer_credential):
    from ctf.services.event import assign_event_staff

    assign_event_staff(
        ctf_event_active.pk, organizer_credential, second_organizer_user.identity_principal.uuid, "co_organizer"
    )
    client = Client()
    client.force_login(second_organizer_user)
    listing = call_json(client, "get", "api_event_content_images", kwargs={"event_id": ctf_event_active.id})
    assert listing.status_code == 200
    assert _get_image(client, images["https://img.example.test/later.png"]).status_code == 200
