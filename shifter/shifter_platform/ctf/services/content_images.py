"""First-party copies of external images in organizer content (#2090, ADR-036).

The portal enforces ``img-src 'self' data:``. When an organizer saves content
with an external image URL (the event logo, rules, challenge descriptions and
solutions, event pages, announcements), :func:`sync_event_content_images`
records one pending row per URL. The CTF scheduler then calls
:func:`ingest_pending_content_images`, which copies each image into platform
storage through the SSRF-guarded pinned HTTPS helper. The SPA shows a ready copy
from the portal origin and shows a link for any other image.

Copy rules (see ``docs/architecture/browser-csp-enforcement-design-2090.md``):
HTTPS only, public addresses, pinned connection, no redirects, a 10 s socket
timeout, a 15 s deadline for every receive of one copy, 5 MiB counted while
reading, and PNG/JPEG/GIF/WebP whose declared type matches its magic bytes.
Transient failures retry with backoff up to three attempts.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ctf.content_images import extract_image_urls, is_admissible_image_url
from ctf.enums import EventCapability
from ctf.services._content_image_authority import can_read_content_image as can_read_content_image
from shared.cloud import get_object_storage
from shared.cloud.exceptions import CloudStorageError
from shared.log_sanitize import safe_log_fingerprint
from shared.uploads.inspection import MagicSignature

if TYPE_CHECKING:
    from uuid import UUID

    from ctf.models import CTFContentImage, CTFParticipant

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10
# Elapsed-time cap for one copy. Every receive ends by this deadline; connect,
# the TLS handshake, and the request send each end within one socket timeout.
COPY_DEADLINE_SECONDS = 15
MAX_ATTEMPTS = 3
INGEST_BATCH_SIZE = 5
INGEST_TIME_BUDGET_SECONDS = 20
_BACKOFF_BASE = timedelta(minutes=5)
_CLAIM_LEASE = timedelta(minutes=2)
_READ_CHUNK_BYTES = 64 * 1024
STORAGE_PREFIX = "ctf-content-images"
# The image collection includes hidden challenges and solutions, so staff need
# exact native event.manage_challenges permission to list or read it.
# Legacy owner, staff and administrator flags grant no authority.
CONTENT_IMAGE_STAFF_CAPABILITY = EventCapability.CHALLENGES.value
# Event states in which participants see challenge solutions (detail projection).
_SOLUTION_VISIBLE_STATUSES = frozenset({"ended", "archived"})

# Declared Content-Type -> accepted magic-byte alternatives (all signatures in
# one alternative must match). SVG is excluded: it can carry script.
_IMAGE_SIGNATURES: dict[str, tuple[tuple[MagicSignature, ...], ...]] = {
    "image/png": ((MagicSignature(0, b"\x89PNG\r\n\x1a\n"),),),
    "image/jpeg": ((MagicSignature(0, b"\xff\xd8\xff"),),),
    "image/gif": ((MagicSignature(0, b"GIF87a"),), (MagicSignature(0, b"GIF89a"),)),
    "image/webp": ((MagicSignature(0, b"RIFF"), MagicSignature(8, b"WEBP")),),
}
_ACCEPT_HEADER = ", ".join(_IMAGE_SIGNATURES)


class FailureCode(StrEnum):
    """Closed vocabulary for a copy that failed or will retry."""

    INSECURE_SCHEME = "insecure_scheme"
    BLOCKED_DESTINATION = "blocked_destination"
    UNREACHABLE = "unreachable"
    REDIRECT = "redirect"
    HTTP_STATUS = "http_status"
    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    STORAGE_ERROR = "storage_error"


class _CopyError(Exception):
    """A copy attempt failed with a closed failure code."""

    def __init__(self, code: FailureCode, *, transient: bool = False) -> None:
        super().__init__(code.value)
        self.code = code
        self.transient = transient


def _digest(url: str) -> str:
    """Return the SHA-256 hex digest that keys a source URL within its event."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def image_url(image_id: UUID) -> str:
    """Return the portal-origin URL that serves a ready copy."""
    return f"/api/v1/ctf/content-images/{image_id}/"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _collect_urls(logo_url: str, texts: list[str]) -> list[str]:
    """Return the unique admissible image URLs of the logo and the texts, in order."""
    found: dict[str, None] = {}
    if logo_url and is_admissible_image_url(logo_url):
        found[logo_url] = None
    for text in texts:
        for url in extract_image_urls(text):
            found.setdefault(url, None)
    return list(found)


def _page_bodies(event_id: UUID) -> list[str]:
    """Return the bodies of the event pages that can show images."""
    # The briefing renders without images (BriefingPage), so it needs no copy.
    from ctf.models import CTFEventPage
    from ctf.models.event import RESERVED_BRIEFING_SLUG

    return list(
        CTFEventPage.objects.filter(event_id=event_id)
        .exclude(slug=RESERVED_BRIEFING_SLUG)
        .values_list("body", flat=True)
    )


def _announcement_bodies(event_id: UUID, *, sent_only: bool) -> list[str]:
    """Return the announcement bodies of the event; only sent ones when ``sent_only``."""
    from ctf.enums import NotificationStatus, NotificationType
    from ctf.models import CTFNotification

    rows = CTFNotification.objects.filter(event_id=event_id, notification_type=NotificationType.ANNOUNCEMENT.value)
    if sent_only:
        rows = rows.filter(status=NotificationStatus.SENT.value)
    return list(rows.values_list("body", flat=True))


def event_image_urls(event_id: UUID) -> list[str]:
    """Return every external image URL the event's organizer content uses."""
    from ctf.models import CTFChallenge, CTFEvent

    event = CTFEvent.objects.filter(pk=event_id).only("logo_url", "rules").first()
    if event is None:
        return []
    texts: list[str] = [event.rules]
    for description, solution in CTFChallenge.objects.filter(event_id=event_id).values_list("description", "solution"):
        texts.extend((description, solution))
    texts += _page_bodies(event_id) + _announcement_bodies(event_id, sent_only=False)
    return _collect_urls(event.logo_url, texts)


def participant_visible_image_urls(participant: CTFParticipant) -> set[str]:
    """Return the external image URLs in content the participant can read now.

    Mirrors the participant read surfaces: event logo and rules, generic pages,
    sent announcements, readable challenges (``assert_challenge_readable_for_participant``),
    and solutions only once the event has ended (the detail projection's rule).
    """
    from ctf.exceptions import CTFStateError, CTFValidationError
    from ctf.models import CTFChallenge
    from ctf.services.challenge import assert_challenge_readable_for_participant

    event = participant.event
    show_solutions = event.status in _SOLUTION_VISIBLE_STATUSES
    texts: list[str] = [event.rules]
    for challenge in CTFChallenge.objects.filter(event_id=event.id):
        try:
            assert_challenge_readable_for_participant(participant, challenge)
        except (CTFStateError, CTFValidationError):
            continue
        texts.append(challenge.description)
        if show_solutions:
            texts.append(challenge.solution)
    texts += _page_bodies(event.id) + _announcement_bodies(event.id, sent_only=True)
    return set(_collect_urls(event.logo_url, texts))


def sync_event_content_images(event_id: UUID) -> None:
    """Make the event's image rows match its content; remove unreferenced copies.

    Syncs for one event run one at a time. Each sync locks the event row, then
    reads the content, so the last sync sees the latest committed content.
    ``FOR NO KEY UPDATE`` leaves inserts of rows that reference the event free.
    Stored copies are deleted only after the row deletion commits.
    """
    from ctf.models import CTFContentImage, CTFEvent

    with transaction.atomic():
        CTFEvent.objects.select_for_update(no_key=True).filter(pk=event_id).values_list("pk", flat=True).first()
        wanted = {_digest(url): url for url in event_image_urls(event_id)}
        existing = dict(CTFContentImage.objects.filter(event_id=event_id).values_list("source_digest", "storage_key"))
        CTFContentImage.objects.bulk_create(
            [
                CTFContentImage(event_id=event_id, source_url=url, source_digest=digest)
                for digest, url in wanted.items()
                if digest not in existing
            ],
            ignore_conflicts=True,
        )
        stale = [digest for digest in existing if digest not in wanted]
        if not stale:
            return
        CTFContentImage.objects.filter(event_id=event_id, source_digest__in=stale).delete()
        for digest in stale:
            if existing[digest]:
                transaction.on_commit(partial(_delete_copy, existing[digest]))


def _delete_copy(storage_key: str) -> None:
    """Delete one stored copy; log a storage failure and continue."""
    try:
        get_object_storage().delete_object(settings.STORAGE_BUCKET_NAME, storage_key)
    except CloudStorageError:
        # Best effort: the row is already gone.
        logger.warning("content image copy delete failed key=%s", safe_log_fingerprint(storage_key))


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def _detect_type(header: bytes) -> str | None:
    """Return the accepted image type whose magic bytes match ``header``, if any."""
    for content_type, alternatives in _IMAGE_SIGNATURES.items():
        if any(all(sig.matches(header) for sig in alternative) for alternative in alternatives):
            return content_type
    return None


def _check_deadline(deadline: float) -> None:
    """Fail the copy as a transient timeout once ``deadline`` has passed."""
    if time.monotonic() > deadline:
        raise _CopyError(FailureCode.TIMEOUT, transient=True)


def _read_bounded(response: http.client.HTTPResponse) -> bytes:
    """Read the body and fail once it exceeds the size limit.

    The transport ends every receive by the copy deadline (``TimeoutError``).
    """
    declared_length = response.getheader("Content-Length")
    if declared_length is not None and declared_length.isdigit() and int(declared_length) > MAX_IMAGE_BYTES:
        raise _CopyError(FailureCode.TOO_LARGE)
    data = bytearray()
    while chunk := response.read(_READ_CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_IMAGE_BYTES:
            raise _CopyError(FailureCode.TOO_LARGE)
    return bytes(data)


def _fetch_image(url: str, deadline: float) -> tuple[bytes, str]:
    """Fetch and validate one image before ``deadline``; return ``(bytes, content_type)``."""
    from ctf.validators import _ssrf

    if not url.lower().startswith("https://"):
        raise _CopyError(FailureCode.INSECURE_SCHEME)
    try:
        with _ssrf.pinned_https_response(
            url, method="GET", timeout=FETCH_TIMEOUT_SECONDS, headers={"Accept": _ACCEPT_HEADER}, deadline=deadline
        ) as response:
            status = int(response.status)
            if 300 <= status < 400:
                raise _CopyError(FailureCode.REDIRECT)
            if status != 200:
                raise _CopyError(FailureCode.HTTP_STATUS, transient=status == 429 or status >= 500)
            declared = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if declared not in _IMAGE_SIGNATURES:
                raise _CopyError(FailureCode.UNSUPPORTED_TYPE)
            data = _read_bounded(response)
    except _ssrf._BlockedDestinationError as exc:
        raise _CopyError(FailureCode.BLOCKED_DESTINATION) from exc
    except TimeoutError as exc:
        raise _CopyError(FailureCode.TIMEOUT, transient=True) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _CopyError(FailureCode.UNREACHABLE, transient=True) from exc
    if _detect_type(data[:16]) != declared:
        raise _CopyError(FailureCode.UNSUPPORTED_TYPE)
    return data, declared


def _store(image: CTFContentImage, data: bytes, content_type: str) -> str:
    """Upload the copy under the image-copy prefix and return its storage key."""
    key = f"{STORAGE_PREFIX}/{image.event_id}/{image.pk}"
    try:
        get_object_storage().upload_file(io.BytesIO(data), settings.STORAGE_BUCKET_NAME, key, content_type)
    except CloudStorageError as exc:
        raise _CopyError(FailureCode.STORAGE_ERROR, transient=True) from exc
    return key


def _claim_next() -> CTFContentImage | None:
    """Claim one due pending row under a short lease (multi-scheduler safe)."""
    from django.db.models import F, Q

    from ctf.models import ContentImageStatus, CTFContentImage

    now = timezone.now()
    with transaction.atomic():
        image = (
            CTFContentImage.objects.select_for_update(skip_locked=True)
            .filter(status=ContentImageStatus.PENDING)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("created_at")
            .first()
        )
        if image is None:
            return None
        # A queryset update takes the lease without model validation, so a row
        # that fails validation still fails alone instead of blocking the queue.
        CTFContentImage.objects.filter(pk=image.pk).update(
            attempts=F("attempts") + 1, next_attempt_at=now + _CLAIM_LEASE, updated_at=now
        )
        image.attempts += 1
    return image


def _copy_one(image: CTFContentImage, deadline: float) -> None:
    """Copy one claimed image before ``deadline`` and record the result on its row."""
    from ctf.models import ContentImageStatus, CTFContentImage

    try:
        data, content_type = _fetch_image(image.source_url, deadline)
        # Never start an upload after the deadline; the copy retries later.
        _check_deadline(deadline)
        key = _store(image, data, content_type)
    except _CopyError as exc:
        retry = exc.transient and image.attempts < MAX_ATTEMPTS
        CTFContentImage.objects.filter(pk=image.pk).update(
            status=ContentImageStatus.PENDING if retry else ContentImageStatus.FAILED,
            failure_code=exc.code.value,
            next_attempt_at=timezone.now() + _BACKOFF_BASE * 2 ** (image.attempts - 1) if retry else None,
            updated_at=timezone.now(),
        )
        logger.info("content image copy failed event=%s code=%s retry=%s", image.event_id, exc.code.value, retry)
        return
    updated = CTFContentImage.objects.filter(pk=image.pk).update(
        status=ContentImageStatus.READY,
        failure_code="",
        next_attempt_at=None,
        storage_key=key,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        updated_at=timezone.now(),
    )
    if not updated:
        # The content stopped referencing the URL while the copy ran.
        _delete_copy(key)


def ingest_pending_content_images(
    *,
    limit: int = INGEST_BATCH_SIZE,
    time_budget_seconds: float = INGEST_TIME_BUDGET_SECONDS,
    heartbeat: Callable[[], None] | None = None,
) -> int:
    """Copy up to ``limit`` due images within the time budget; return the count attempted.

    Each copy runs under its own elapsed-time deadline, capped by the batch budget.
    ``heartbeat`` runs after every copy so the scheduler's liveness probe stays fresh.
    """
    budget_end = time.monotonic() + time_budget_seconds
    attempted = 0
    while attempted < limit and time.monotonic() < budget_end:
        image = _claim_next()
        if image is None:
            break
        _copy_one(image, min(budget_end, time.monotonic() + COPY_DEADLINE_SECONDS))
        attempted += 1
        if heartbeat is not None:
            heartbeat()
    return attempted


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def content_image_map(event_id: UUID) -> dict[str, str]:
    """Return ``{source_url: portal URL}`` for the event's ready copies."""
    from ctf.models import ContentImageStatus, CTFContentImage

    rows = CTFContentImage.objects.filter(event_id=event_id, status=ContentImageStatus.READY).values_list(
        "source_url", "id"
    )
    return {source_url: image_url(image_id) for source_url, image_id in rows}


def participant_image_state(participant: CTFParticipant) -> tuple[dict[str, str], int]:
    """Return the ready copies and the pending count for content the participant can read now.

    The pending count lets the SPA poll until copies finish without revealing the
    source URLs of copies that are not ready.
    """
    from ctf.models import ContentImageStatus, CTFContentImage

    visible = participant_visible_image_urls(participant)
    ready = {url: path for url, path in content_image_map(participant.event_id).items() if url in visible}
    pending_urls = CTFContentImage.objects.filter(
        event_id=participant.event_id, status=ContentImageStatus.PENDING
    ).values_list("source_url", flat=True)
    return ready, sum(1 for url in pending_urls if url in visible)


def participant_image_map(participant: CTFParticipant) -> dict[str, str]:
    """Return the ready copies of images in content the participant can read now."""
    return participant_image_state(participant)[0]


def read_content_image(image: CTFContentImage) -> bytes | None:
    """Return the stored bytes of a ready copy, or ``None`` if they do not verify."""
    try:
        data = get_object_storage().read_object_header(
            settings.STORAGE_BUCKET_NAME, image.storage_key, image.size_bytes + 1
        )
    except CloudStorageError:
        # A missing or unreadable copy is a not-found result.
        logger.warning("content image read failed image=%s", image.pk)
        return None
    if len(data) != image.size_bytes or hashlib.sha256(data).hexdigest() != image.sha256:
        logger.warning("content image integrity mismatch image=%s", image.pk)
        return None
    return data
