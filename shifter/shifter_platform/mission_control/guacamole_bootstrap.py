"""Asynchronous Guacamole URL bootstrap runner."""

from __future__ import annotations

import atexit
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from threading import BoundedSemaphore
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from mission_control.models import GuacamoleBootstrapRequest
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.errors import classify_user_message
from shared.log_sanitize import safe_log_fingerprint, safe_log_value

if TYPE_CHECKING:
    from .guacamole import GuacamoleClient

logger = logging.getLogger(__name__)

_DEFAULT_WORKERS = 4
_DEFAULT_TTL_SECONDS = 300
_DEFAULT_PRUNE_BATCH_SIZE = 500
_GENERIC_BOOTSTRAP_FAILURE = "Guacamole session bootstrap failed"

_slot_limit: int | None = None
_slots: BoundedSemaphore | None = None
_executor: ThreadPoolExecutor | None = None


class BootstrapQueueFull(Exception):
    """Raised when Guacamole bootstrap workers are already occupied."""


class BootstrapFailure(Exception):
    """Raised by a bootstrap builder for an expected user-facing failure."""

    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = _normalise_status_code(status_code)
        # Select from authored messages before anything is persisted or reaches
        # an API response. Exact established messages are retained by the
        # classifier; an unexpected caller-provided value becomes generic.
        self.user_message = classify_user_message(
            message,
            default=_GENERIC_BOOTSTRAP_FAILURE,
        )


def _normalise_status_code(status_code: int) -> int:
    """Clamp persisted failure codes to HTTP error statuses."""
    if 400 <= status_code <= 599:
        return status_code
    return 500


def _clean_error_message(message: str) -> str:
    """Return a bounded single-line error string for polling clients."""
    cleaned = message.replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:500] or _GENERIC_BOOTSTRAP_FAILURE


def _ttl_seconds() -> int:
    """Return the configured bootstrap record lifetime."""
    raw_value = int(getattr(settings, "GUACAMOLE_BOOTSTRAP_TTL_SECONDS", _DEFAULT_TTL_SECONDS))
    return max(30, raw_value)


def _worker_limit() -> int:
    """Return the configured per-process worker limit."""
    return max(1, int(getattr(settings, "GUACAMOLE_BOOTSTRAP_WORKERS", _DEFAULT_WORKERS)))


def _get_slots() -> BoundedSemaphore:
    """Return the semaphore that bounds in-process bootstrap concurrency."""
    global _slot_limit, _slots
    worker_limit = _worker_limit()
    if _slots is None or _slot_limit != worker_limit:
        _slot_limit = worker_limit
        _slots = BoundedSemaphore(worker_limit)
    return _slots


def _get_executor() -> ThreadPoolExecutor:
    """Return the lazily-created bootstrap worker pool."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_worker_limit(), thread_name_prefix="guacamole-bootstrap")
        atexit.register(_executor.shutdown, wait=False)
    return _executor


def _user_id_from_value(value: int | str) -> int:
    """Normalize Django user identifiers for storage."""
    return int(value)


def enqueue_guacamole_bootstrap(
    *,
    user_id: int | str | None,
    protocol: str,
    target_id: str,
    build_url: Callable[[], str],
    authority: dict[str, object] | None = None,
    guacamole_client: GuacamoleClient | None = None,
    bootstrap_id: UUID | None = None,
) -> GuacamoleBootstrapRequest:
    """Create a bootstrap request and run the blocking token exchange off-path."""
    slots = _get_slots()
    if not slots.acquire(blocking=False):
        raise BootstrapQueueFull

    try:
        bootstrap = GuacamoleBootstrapRequest.objects.create(
            id=bootstrap_id or uuid4(),
            user_id=_user_id_from_value(user_id) if user_id is not None else None,
            protocol=protocol,
            target_id=str(target_id)[:200],
            status=GuacamoleBootstrapRequest.Status.PENDING,
            expires_at=timezone.now() + timedelta(seconds=_ttl_seconds()),
            **(authority or {}),
        )
    except Exception:
        slots.release()
        raise

    if getattr(settings, "GUACAMOLE_BOOTSTRAP_INLINE", False):
        _run_bootstrap(bootstrap.id, build_url, slots, guacamole_client)
        bootstrap.refresh_from_db()
        return bootstrap

    try:
        _get_executor().submit(_run_bootstrap, bootstrap.id, build_url, slots, guacamole_client)
    except Exception:
        slots.release()
        bootstrap.status = GuacamoleBootstrapRequest.Status.FAILED
        bootstrap.error_message = "Guacamole bootstrap workers unavailable"
        bootstrap.error_status_code = 503
        bootstrap.save(update_fields=("status", "error_message", "error_status_code", "updated_at"))
        raise
    return bootstrap


def _run_bootstrap(
    request_id: UUID,
    build_url: Callable[[], str],
    slots: BoundedSemaphore,
    guacamole_client: GuacamoleClient | None = None,
) -> None:
    """Run a single blocking Guacamole URL build and persist the result."""
    close_old_connections()
    started = time.perf_counter()
    try:
        bootstrap = GuacamoleBootstrapRequest.objects.get(pk=request_id)
        if bootstrap.authority_kind:
            from .guacamole_authority import authorize_bootstrap

            if not authorize_bootstrap(bootstrap):
                _finish_failure(bootstrap, started, "Permission denied", 403)
                return
        bootstrap.status = GuacamoleBootstrapRequest.Status.RUNNING
        bootstrap.save(update_fields=("status", "updated_at"))

        result_url = _build_bootstrap_url(bootstrap, build_url, started)
        if result_url is None:
            return
        if not _bootstrap_can_finish(bootstrap, result_url, started, guacamole_client):
            return

        duration_ms = _duration_ms(started)
        bootstrap.status = GuacamoleBootstrapRequest.Status.SUCCEEDED
        bootstrap.result_url = result_url
        bootstrap.error_message = ""
        bootstrap.error_status_code = 500
        bootstrap.duration_ms = duration_ms
        bootstrap.save(
            update_fields=(
                "status",
                "result_url",
                "error_message",
                "error_status_code",
                "duration_ms",
                "updated_at",
            )
        )
        logger.info(
            "Guacamole bootstrap succeeded: request_id=%s protocol=%s target_id=%s duration_ms=%s",
            request_id,
            safe_log_value(bootstrap.protocol),
            safe_log_value(bootstrap.target_id),
            duration_ms,
        )
    except GuacamoleBootstrapRequest.DoesNotExist:
        logger.warning("Guacamole bootstrap request disappeared before execution: request_id=%s", request_id)
    finally:
        slots.release()
        close_old_connections()


def _finish_failure(
    bootstrap: GuacamoleBootstrapRequest,
    started: float,
    message: str,
    status_code: int,
) -> None:
    """Mark the bootstrap request FAILED, persisting the error details and duration."""
    duration_ms = _duration_ms(started)
    bootstrap.status = GuacamoleBootstrapRequest.Status.FAILED
    bootstrap.error_message = _clean_error_message(message)
    bootstrap.error_status_code = _normalise_status_code(status_code)
    bootstrap.duration_ms = duration_ms
    bootstrap.save(update_fields=("status", "error_message", "error_status_code", "duration_ms", "updated_at"))
    logger.warning(
        "Guacamole bootstrap failed: request_id=%s protocol=%s target_id=%s duration_ms=%s status_code=%s",
        bootstrap.id,
        safe_log_value(bootstrap.protocol),
        safe_log_value(bootstrap.target_id),
        duration_ms,
        bootstrap.error_status_code,
    )


def _duration_ms(started: float) -> int:
    """Return elapsed milliseconds since a monotonic start time."""
    return max(0, int((time.perf_counter() - started) * 1000))


def _prune_batch_size() -> int:
    """Return the configured bounded prune batch size."""
    return max(1, int(getattr(settings, "GUACAMOLE_BOOTSTRAP_PRUNE_BATCH_SIZE", _DEFAULT_PRUNE_BATCH_SIZE)))


def consume_ready_url(
    *, request_id: UUID, user_id: int | None = None, credential: CredentialContext | None = None
) -> str | None:
    """Atomically deliver and clear the ready Guacamole URL exactly once.

    The status endpoint is the delivery boundary: the first original-proof poll
    that finds a ready URL consumes it and clears the token material from the
    row in the same transaction, so a repeated poll (a second tab, the opener
    plus the JS client, a retry) can never replay the same token. Returns the
    URL on the single delivering call; returns ``None`` when the row is not
    deliverable for this caller — not found, not succeeded, expired, or already
    delivered — clearing any token still parked on an expired success.
    """
    admitted = _delivery_actor(request_id, credential)
    if admitted is None:
        return None
    with transaction.atomic():
        from cms.services import assert_remote_authority_binding

        fields = dict(admitted.binding)
        try:
            assert_remote_authority_binding(fields, str(fields["protocol"]), str(fields["target_id"]))
        except Exception:
            return None
        row = GuacamoleBootstrapRequest.objects.select_for_update().filter(pk=request_id).first()
        if row is None or row.status != GuacamoleBootstrapRequest.Status.SUCCEEDED:
            return None
        try:
            if (
                _delivery_binding(row) != admitted.binding
                or resolve_local_credential(admitted.credential) != admitted.credential
                or resolve_local_credential(admitted.original) != admitted.original
            ):
                return None
        except ValueError:
            return None
        return _consume_locked_url(row, credential)


def prune_expired_bootstrap_requests(*, batch_size: int | None = None) -> int:
    """Delete expired bootstrap rows in a bounded batch; return the count.

    Expired rows are no longer usable by any client, so deleting them removes
    any token URL still parked on an abandoned ``succeeded`` row and bounds
    table growth. The delete is bounded by ``batch_size`` and ordered oldest
    first; non-expired ``pending`` / ``running`` work is never touched.
    """
    limit = _prune_batch_size() if batch_size is None else max(1, int(batch_size))
    ids = list(
        GuacamoleBootstrapRequest.objects.filter(
            expires_at__lte=timezone.now(),
            # nosec B106
            native_session_token="",
        )
        .filter(Q(authority_kind="") | Q(status="failed") | Q(native_terminated_at__isnull=False))
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    if not ids:
        return 0
    deleted, _details = GuacamoleBootstrapRequest.objects.filter(pk__in=ids).delete()
    return deleted


def _build_bootstrap_url(
    bootstrap: GuacamoleBootstrapRequest, build_url: Callable[[], str], started: float
) -> str | None:
    """Record sanitized build failures without exposing upstream credential material."""
    try:
        result_url = build_url()
    except BootstrapFailure as exc:
        _finish_failure(bootstrap, started, exc.user_message, exc.status_code)
        return None
    except Exception as exc:
        # Keep the original exception text out of logs: Guacamole failures
        # can contain credential-bearing upstream values. ``exc_info=False``
        # preserves the sanitized fingerprint without appending that text.
        logger.exception(
            "Guacamole bootstrap failed: request_id=%s reason=%s",
            safe_log_value(bootstrap.pk),
            safe_log_fingerprint(exc),
            exc_info=False,
        )
        _finish_failure(bootstrap, started, _GENERIC_BOOTSTRAP_FAILURE, 500)
        return None

    return result_url


def _bootstrap_can_finish(
    bootstrap: GuacamoleBootstrapRequest,
    result_url: str,
    started: float,
    guacamole_client: GuacamoleClient | None,
) -> bool:
    """Retain native cleanup custody and refuse a URL whose admitted handoff has expired."""
    if bootstrap.authority_kind and not _retain_native_bootstrap(bootstrap, result_url, started, guacamole_client):
        return False

    if bootstrap.is_expired:
        # A slow build finished after expiry. Never persist a token URL the
        # status endpoint would refuse to deliver (it returns 410 for an
        # expired row); record the expiry instead so no token is parked at
        # rest until pruning.
        _finish_failure(bootstrap, started, "Guacamole session request expired", 410)
        return False

    return True


def _retain_native_bootstrap(
    bootstrap: GuacamoleBootstrapRequest,
    result_url: str,
    started: float,
    guacamole_client: GuacamoleClient | None,
) -> bool:
    """Persist the minted token before reauthorization so failed handoffs remain independently terminable."""
    from .guacamole_authority import authorize_bootstrap

    # Retain the native credential before checking authority again. If
    # revoke/cancel races token creation, independent cleanup can close
    # that session without delivering it to the browser.
    query = urlparse(result_url).fragment.partition("?")[2]
    tokens = parse_qs(query).get("token", [])
    if len(tokens) != 1 or not tokens[0]:
        _finish_failure(bootstrap, started, _GENERIC_BOOTSTRAP_FAILURE, 500)
        return False
    bootstrap.native_session_token = tokens[0]
    bootstrap.save(update_fields=("native_session_token", "updated_at"))
    if bootstrap.is_expired or not authorize_bootstrap(bootstrap):
        from .guacamole_authority import terminate_bootstrap

        bootstrap.termination_pending = True
        bootstrap.save(update_fields=("termination_pending", "updated_at"))
        if guacamole_client is not None:
            terminate_bootstrap(bootstrap, guacamole_client)
        _finish_failure(bootstrap, started, "Permission denied", 403)
        return False
    return True


@dataclass(frozen=True, slots=True)
class _DeliveryAdmission:
    credential: CredentialContext
    original: CredentialContext
    binding: tuple[tuple[str, object], ...]


def _delivery_binding(row: GuacamoleBootstrapRequest) -> tuple[tuple[str, object], ...]:
    """Capture the exact server-owned proof, target and resource generation."""
    names: tuple[str, ...] = ("id", "user_id", "protocol", "target_id")
    names += tuple(
        field.name
        for field in row._meta.concrete_fields
        if field.name.startswith(("authority_", "credential_", "initiating_principal_"))
    )
    return tuple((name, getattr(row, name)) for name in names)


def _delivery_actor(request_id: UUID, credential: CredentialContext | None) -> _DeliveryAdmission | None:
    """Reauthorize the original principal proof before entering the browser handoff transaction."""
    from shared.operation_authority import credential_reference_from_record

    from .guacamole_authority import authorize_bootstrap

    if not isinstance(credential, CredentialContext):
        return None
    try:
        current = resolve_current_credential(credential)
        snapshot = (
            GuacamoleBootstrapRequest.objects.defer("result_url", "native_session_token").filter(pk=request_id).first()
        )
        if snapshot is None or snapshot.authority_kind != "principal" or not authorize_bootstrap(snapshot, current):
            return None
        original = resolve_current_credential(credential_reference_from_record(snapshot))
        return _DeliveryAdmission(current, original, _delivery_binding(snapshot))
    except ValueError:
        return None


def _consume_locked_url(row: GuacamoleBootstrapRequest, credential: CredentialContext | None) -> str | None:
    """Clear a succeeded URL exactly once while preserving undelivered native cleanup evidence."""
    if row.authority_kind and credential is None:
        return None
    if row.is_expired or row.delivered_at is not None or not row.result_url:
        # Not deliverable: clear any token still parked on an expired success
        # before refusing, so it does not sit at rest until pruning.
        if row.result_url:
            row.result_url = ""
            row.save(update_fields=("result_url", "updated_at"))
        return None
    url = row.result_url
    row.result_url = ""
    row.delivered_at = timezone.now()
    row.save(update_fields=("result_url", "delivered_at", "updated_at"))
    return url
