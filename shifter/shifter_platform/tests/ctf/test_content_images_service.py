"""First-party copies of external images in organizer content (#2090, ADR-036).

The enforced CSP admits only ``img-src 'self' data:``. When an organizer saves
content with an external image URL, the platform copies the image into its own
storage and the SPA shows the copy. These tests drive sync, the SSRF-guarded copy
through the real pinned request helper, and the image map.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import timedelta

import pytest
from django.utils import timezone

from ctf.enums import ChallengeCategory, ChallengeDifficulty, NotificationType
from ctf.models import ContentImageStatus, CTFChallenge, CTFContentImage, CTFEventPage, CTFNotification
from ctf.services import content_images as svc
from ctf.validators import _ssrf
from shared.cloud.exceptions import CloudStorageError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail_upload = False

    def upload_file(self, file_obj, bucket, key, content_type=""):
        if self.fail_upload:
            raise CloudStorageError("storage down")
        self.objects[key] = (file_obj.read(), content_type)

    def read_object_header(self, bucket, key, max_bytes, *, expected_identity=None):
        return self.objects[key][0][:max_bytes]

    def delete_object(self, bucket, key):
        self.objects.pop(key, None)


class FakeResponse:
    def __init__(self, status=200, body=PNG, headers=None) -> None:
        self.status = status
        self._body = body
        self._headers = {"content-type": "image/png", **{k.lower(): v for k, v in (headers or {}).items()}}

    def getheader(self, name, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, amt=None):
        chunk, self._body = self._body[:amt], self._body[amt:]
        return chunk


class FakeConnection:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error

    def request(self, method, target, *, body, headers):
        if self.error:
            raise self.error

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.fixture
def storage(monkeypatch, settings):
    settings.STORAGE_BUCKET_NAME = "synthetic-bucket"
    fake = FakeStorage()
    monkeypatch.setattr(svc, "get_object_storage", lambda: fake)
    return fake


@pytest.fixture
def remote(monkeypatch):
    """Serve one fake response through the real pinned helper; record connections."""
    state = {"response": FakeResponse(), "error": None, "connections": 0}

    def build(**_kwargs):
        state["connections"] += 1
        # Each connection reads its own copy of the response body.
        return FakeConnection(copy.copy(state["response"]), state["error"])

    monkeypatch.setattr(_ssrf, "_resolve_and_validate", lambda hostname, port: ["93.184.216.34"])
    monkeypatch.setattr(_ssrf, "_build_https_connection", build)
    return state


def _row(event, url):
    return CTFContentImage.objects.get(event=event, source_digest=hashlib.sha256(url.encode()).hexdigest())


@pytest.mark.django_db
def test_sync_tracks_every_organizer_content_source(ctf_event):
    ctf_event.logo_url = "https://img.example.test/logo.png"
    ctf_event.rules = "![r](https://img.example.test/rules.png) ![rel](/static/a.png)"
    ctf_event.save()
    CTFChallenge.objects.create(
        event=ctf_event,
        name="Pictures",
        description="![d](https://img.example.test/desc.png)",
        solution="![s](https://img.example.test/sol.png)",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
    )
    CTFEventPage.objects.create(
        event=ctf_event, title="Info", slug="info", body="![p](https://img.example.test/page.png)"
    )
    CTFEventPage.objects.create(
        event=ctf_event, title="Briefing", slug="briefing", body="![b](https://img.example.test/briefing.png)"
    )
    CTFNotification.objects.create(
        event=ctf_event,
        notification_type=NotificationType.ANNOUNCEMENT.value,
        created_by=ctf_event.created_by,
        subject="Hi",
        body="![a](https://img.example.test/ann.png) ![dup](https://img.example.test/logo.png)",
    )

    svc.sync_event_content_images(ctf_event.id)

    urls = set(CTFContentImage.objects.filter(event=ctf_event).values_list("source_url", flat=True))
    assert urls == {
        "https://img.example.test/logo.png",
        "https://img.example.test/rules.png",
        "https://img.example.test/desc.png",
        "https://img.example.test/sol.png",
        "https://img.example.test/page.png",
        "https://img.example.test/ann.png",
    }
    assert set(CTFContentImage.objects.values_list("status", flat=True)) == {ContentImageStatus.PENDING}


@pytest.mark.django_db
def test_sync_removes_images_no_longer_referenced_and_their_copies(
    ctf_event, storage, remote, django_capture_on_commit_callbacks
):
    ctf_event.rules = "![a](https://img.example.test/a.png)"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    assert svc.ingest_pending_content_images() == 1
    key = _row(ctf_event, "https://img.example.test/a.png").storage_key
    assert key in storage.objects

    ctf_event.rules = "No images now."
    ctf_event.save()
    with django_capture_on_commit_callbacks() as callbacks:
        svc.sync_event_content_images(ctf_event.id)

    assert not CTFContentImage.objects.filter(event=ctf_event).exists()
    # The stored copy is deleted only after the row deletion commits.
    assert key in storage.objects
    for callback in callbacks:
        callback()
    assert key not in storage.objects


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "content_type"),
    [(PNG, "image/png"), (GIF, "image/gif"), (WEBP, "image/webp"), (JPEG, "image/jpeg")],
)
def test_copies_a_valid_image_into_platform_storage(ctf_event, storage, remote, body, content_type):
    url = "https://img.example.test/picture"
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    remote["response"] = FakeResponse(body=body, headers={"Content-Type": f"{content_type}; charset=binary"})

    assert svc.ingest_pending_content_images() == 1

    row = _row(ctf_event, url)
    assert row.status == ContentImageStatus.READY
    assert row.content_type == content_type
    assert row.size_bytes == len(body)
    assert row.sha256 == hashlib.sha256(body).hexdigest()
    assert storage.objects[row.storage_key] == (body, content_type)
    assert row.storage_key.startswith(f"ctf-content-images/{ctf_event.id}/")
    assert svc.content_image_map(ctf_event.id) == {url: f"/api/v1/ctf/content-images/{row.id}/"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("url", "response", "error", "code"),
    [
        ("http://img.example.test/a.png", FakeResponse(), None, "insecure_scheme"),
        ("https://img.example.test/a.png", FakeResponse(status=302), None, "redirect"),
        ("https://img.example.test/a.png", FakeResponse(status=404), None, "http_status"),
        (
            "https://img.example.test/a.png",
            FakeResponse(headers={"Content-Type": "text/html"}),
            None,
            "unsupported_type",
        ),
        (
            "https://img.example.test/a.svg",
            FakeResponse(body=b"<svg onload=alert(1)>", headers={"Content-Type": "image/svg+xml"}),
            None,
            "unsupported_type",
        ),
        ("https://img.example.test/a.png", FakeResponse(body=GIF), None, "unsupported_type"),
        (
            "https://img.example.test/a.png",
            FakeResponse(body=PNG + b"\x00" * svc.MAX_IMAGE_BYTES),
            None,
            "too_large",
        ),
        (
            "https://img.example.test/a.png",
            FakeResponse(headers={"Content-Length": str(svc.MAX_IMAGE_BYTES + 1)}),
            None,
            "too_large",
        ),
    ],
)
def test_rejected_images_fail_without_a_stored_copy(ctf_event, storage, remote, url, response, error, code):
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    remote["response"], remote["error"] = response, error

    svc.ingest_pending_content_images()

    row = _row(ctf_event, url)
    assert row.status == ContentImageStatus.FAILED
    assert row.failure_code == code
    assert storage.objects == {}
    assert svc.content_image_map(ctf_event.id) == {}
    if code == "insecure_scheme":
        assert remote["connections"] == 0


@pytest.mark.django_db
def test_blocked_destinations_never_connect(ctf_event, storage, monkeypatch):
    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("169.254.169.254", 443))])
    monkeypatch.setattr(_ssrf, "_build_https_connection", lambda **_kw: pytest.fail("connected to a blocked address"))
    ctf_event.rules = "![x](https://img.example.test/a.png)"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)

    svc.ingest_pending_content_images()

    row = _row(ctf_event, "https://img.example.test/a.png")
    assert (row.status, row.failure_code) == (ContentImageStatus.FAILED, "blocked_destination")


@pytest.mark.django_db
def test_transient_failures_retry_with_backoff_then_fail(ctf_event, storage, remote):
    url = "https://img.example.test/a.png"
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    remote["error"] = OSError("connection reset")

    svc.ingest_pending_content_images()
    row = _row(ctf_event, url)
    assert (row.status, row.failure_code, row.attempts) == (ContentImageStatus.PENDING, "unreachable", 1)
    assert row.next_attempt_at > timezone.now()
    # Not due yet: a second sweep does not retry early.
    assert svc.ingest_pending_content_images() == 0

    for _ in range(svc.MAX_ATTEMPTS - 1):
        CTFContentImage.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        svc.ingest_pending_content_images()
    row.refresh_from_db()
    assert (row.status, row.attempts) == (ContentImageStatus.FAILED, svc.MAX_ATTEMPTS)


@pytest.mark.django_db
def test_storage_failure_is_transient(ctf_event, storage, remote):
    url = "https://img.example.test/a.png"
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    storage.fail_upload = True

    svc.ingest_pending_content_images()

    row = _row(ctf_event, url)
    assert (row.status, row.failure_code) == (ContentImageStatus.PENDING, "storage_error")


@pytest.mark.django_db
def test_a_malformed_row_fails_alone_and_never_blocks_the_queue(ctf_event, storage, remote):
    """A bad row from older data cannot stall later copies (core-F4, #2090)."""
    ctf_event.rules = "![x](https://img.example.test/good.png)"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    bad = CTFContentImage.objects.bulk_create(
        [CTFContentImage(event=ctf_event, source_url="https://exa_mple..test/a.png", source_digest="0" * 64)]
    )[0]
    # Oldest first: the malformed row is claimed before the valid one.
    CTFContentImage.objects.filter(pk=bad.pk).update(created_at=timezone.now() - timedelta(days=1))

    assert svc.ingest_pending_content_images() == 2

    good = _row(ctf_event, "https://img.example.test/good.png")
    assert good.status == ContentImageStatus.READY
    assert not CTFContentImage.objects.filter(pk=bad.pk, status=ContentImageStatus.PENDING).exists()


@pytest.mark.django_db
def test_sync_ignores_an_over_long_url_without_error(ctf_event):
    long_url = "https://img.example.test/" + "a" * 2100 + ".png"
    ctf_event.rules = f"![long]({long_url}) ![ok](https://img.example.test/ok.png)"
    ctf_event.save()

    svc.sync_event_content_images(ctf_event.id)

    assert list(CTFContentImage.objects.filter(event=ctf_event).values_list("source_url", flat=True)) == [
        "https://img.example.test/ok.png"
    ]


class _Clock:
    """Monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class _DeadlineResponse(FakeResponse):
    """A response whose transport reaches the copy deadline on the second read."""

    reads = 0

    def read(self, amt=None):
        self.reads += 1
        if self.reads > 1:
            raise TimeoutError("request deadline exceeded")
        return super().read(amt)


@pytest.mark.django_db
def test_a_copy_that_reaches_its_deadline_retries_as_a_timeout(ctf_event, storage, remote, monkeypatch):
    """The copy passes its deadline to the transport and retries a late copy (core-F1, #2090)."""
    url = "https://img.example.test/slow.png"
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    remote["response"] = _DeadlineResponse(body=PNG + b"\x00" * svc._READ_CHUNK_BYTES)
    deadlines: list[float | None] = []
    real_pinned = _ssrf.pinned_https_response

    def pinned(url, **kwargs):
        deadlines.append(kwargs.get("deadline"))
        return real_pinned(url, **kwargs)

    monkeypatch.setattr(_ssrf, "pinned_https_response", pinned)
    started = svc.time.monotonic()

    svc.ingest_pending_content_images()

    row = _row(ctf_event, url)
    assert (row.status, row.failure_code) == (ContentImageStatus.PENDING, "timeout")
    assert storage.objects == {}
    assert len(deadlines) == 1
    assert deadlines[0] is not None
    assert started < deadlines[0] <= svc.time.monotonic() + svc.COPY_DEADLINE_SECONDS


@pytest.mark.django_db
def test_no_upload_starts_after_the_deadline(ctf_event, storage, remote, monkeypatch):
    url = "https://img.example.test/late.png"
    ctf_event.rules = f"![x]({url})"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    real_fetch = svc._fetch_image
    clock = _Clock(step=0.0)
    monkeypatch.setattr(svc.time, "monotonic", clock)

    def fetch_then_late(source_url, deadline):
        result = real_fetch(source_url, deadline)
        clock.now = deadline + 1
        return result

    monkeypatch.setattr(svc, "_fetch_image", fetch_then_late)
    monkeypatch.setattr(storage, "upload_file", lambda *_a, **_k: pytest.fail("upload started after the deadline"))

    svc.ingest_pending_content_images()

    assert _row(ctf_event, url).failure_code == "timeout"


@pytest.mark.django_db
def test_ingest_touches_the_heartbeat_after_each_copy(ctf_event, storage, remote):
    ctf_event.rules = "![a](https://img.example.test/a.png) ![b](https://img.example.test/b.png)"
    ctf_event.save()
    svc.sync_event_content_images(ctf_event.id)
    beats: list[int] = []

    assert svc.ingest_pending_content_images(heartbeat=lambda: beats.append(1)) == 2
    assert beats == [1, 1]
