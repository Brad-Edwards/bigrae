"""Outbound webhooks for event milestones (CTF-1203).

Deliveries run on a small background thread pool (same pattern as
:mod:`shared.email`): the triggering action never blocks on, or fails
because of, a receiver. Each delivery retries with exponential backoff
inside its worker before recording a final status on the webhook row.
Payloads carry the webhook event type, an ISO timestamp, and entity data;
a per-webhook secret yields an ``X-Shifter-Signature`` HMAC-SHA256 header.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from ctf.models import CTFEvent, CTFWebhook

logger = logging.getLogger(__name__)

WEBHOOK_EVENT_TYPES = frozenset({"flag_solve", "first_blood", "event_state_change", "participant_registered"})


def create_event_webhook(
    event: CTFEvent,
    *,
    url: str,
    secret: str,
    subscribed_events: list[str],
    actor_id: int,
) -> CTFWebhook:
    """Register a webhook on an event, asserting the ``config`` capability (#1922).

    The mutation lives behind this service boundary rather than in the view so
    the event policy is enforced at the final write, not only at the HTTP
    resolver.
    """
    from ctf.enums import EventCapability
    from ctf.exceptions import CTFValidationError
    from ctf.models import CTFWebhook
    from ctf.services.authorization import assert_event_capability
    from ctf.validators._ssrf import is_blocked_url

    assert_event_capability(actor_id, event, EventCapability.CONFIG)
    if not isinstance(url, str) or not url.startswith("https://") or is_blocked_url(url):
        raise CTFValidationError("Webhook destination is unavailable.", code="CTF_WEBHOOK_DESTINATION_BLOCKED")
    return CTFWebhook.objects.create(event=event, url=url, secret=secret, subscribed_events=subscribed_events)


def delete_event_webhook(webhook_id: UUID, *, actor_id: int) -> None:
    """Soft-delete a webhook, asserting the ``config`` capability on its event (#1922).

    Raises:
        CTFNotFoundError: If the webhook does not exist.
        CTFPermissionError: If ``actor_id`` lacks the ``config`` capability.
    """
    from ctf.enums import EventCapability
    from ctf.exceptions import CTFNotFoundError
    from ctf.models import CTFWebhook
    from ctf.services.authorization import assert_event_capability

    webhook = CTFWebhook.objects.select_related("event").filter(pk=webhook_id, deleted_at__isnull=True).first()
    if webhook is None:
        raise CTFNotFoundError("Webhook not found", details={"webhook_id": str(webhook_id)})
    assert_event_capability(actor_id, webhook.event, EventCapability.CONFIG)
    webhook.delete(soft=True)


_DELIVERY_TIMEOUT_SECONDS = 10
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 5

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ctf-webhook")


def _post_pinned(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST over TLS to one validated IP; redirects never become new requests."""
    from ctf.validators._ssrf import pinned_https_response

    with pinned_https_response(
        url, method="POST", timeout=_DELIVERY_TIMEOUT_SECONDS, headers=headers, body=body
    ) as response:
        return int(response.status)


def emit_webhook(event: CTFEvent, event_type: str, data: dict[str, Any]) -> int:
    """Queue delivery of one milestone to every subscribed webhook.

    Returns the number of deliveries queued. Never raises: webhook problems
    must not affect the triggering action.
    """
    from django.utils import timezone

    from ctf.models import CTFWebhook

    try:
        webhooks = [
            hook
            for hook in CTFWebhook.objects.filter(event=event, active=True, deleted_at__isnull=True)
            if not hook.subscribed_events or event_type in hook.subscribed_events
        ]
        if not webhooks:
            return 0
        body = json.dumps(
            {
                "event_type": event_type,
                "timestamp": timezone.now().isoformat(),
                "ctf_event": {"id": str(event.pk), "name": event.name},
                "data": data,
            },
            default=str,
        ).encode()
        for hook in webhooks:
            _executor.submit(_deliver_with_retries, hook.pk, hook.url, hook.secret, body, event_type)
        return len(webhooks)
    except Exception:
        logger.exception("Failed to queue %s webhooks for event %s", event_type, event.pk)
        return 0


def _delivery_is_current(webhook_pk: UUID, url: str, secret: str, event_type: str | None) -> bool:
    """Deny a queued send after the subscription or event binding changes."""
    from ctf.models import CTFWebhook

    row = CTFWebhook.objects.filter(
        pk=webhook_pk, active=True, deleted_at__isnull=True, event__deleted_at__isnull=True
    ).first()
    return bool(
        row is not None
        and row.url == url
        # Compare UTF-8 bytes: compare_digest raises TypeError for non-ASCII str values.
        and hmac.compare_digest(row.secret.encode("utf-8"), secret.encode("utf-8"))
        and (event_type is None or not row.subscribed_events or event_type in row.subscribed_events)
    )


def _attempt_delivery(
    webhook_pk: UUID, url: str, secret: str, body: bytes, event_type: str | None, headers: dict[str, str]
) -> str:
    """Return one current subscription's HTTP outcome, or refuse a revoked one."""
    if not _delivery_is_current(webhook_pk, url, secret, event_type):
        return "failed:revoked"
    status = _post_pinned(url, body, headers)
    return f"{'ok' if 200 <= status < 300 else 'failed'}:{status}"


def _deliver_with_retries(webhook_pk: UUID, url: str, secret: str, body: bytes, event_type: str | None = None) -> None:
    """Recheck the live subscription before each pinned POST and record the outcome."""
    from django.db import DatabaseError

    from ctf.validators._ssrf import _BlockedDestinationError

    headers = {"Content-Type": "application/json"}
    if secret:
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Shifter-Signature"] = f"sha256={signature}"
        headers["X-BigRAE-Signature"] = headers["X-Shifter-Signature"]

    status = "failed"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            status = _attempt_delivery(webhook_pk, url, secret, body, event_type, headers)
            if status.startswith("ok:") or status == "failed:revoked":
                break
        except _BlockedDestinationError:
            status = "failed:blocked_destination"
            break
        except DatabaseError:
            status = "failed:unavailable"
            break
        except (OSError, http.client.HTTPException):
            status = "failed:transport"
        # delivery errors may carry the webhook URL, which can embed a token
        except Exception as exc:  # noqa: BLE001
            # Thread-pool boundary: an unexpected error must not end the delivery without a record.
            logger.error(
                "Webhook %s delivery failed unexpectedly exc_type=%s", webhook_pk, type(exc).__name__, exc_info=False
            )
            status = "failed"
            break
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_BACKOFF_BASE_SECONDS**attempt)
    _record_delivery(webhook_pk, status)


def _record_delivery(webhook_pk: UUID, status: str) -> None:
    """Persist the delivery outcome; best-effort (worker thread)."""
    from django.utils import timezone

    from ctf.models import CTFWebhook

    try:
        CTFWebhook.objects.filter(pk=webhook_pk).update(last_status=status, last_delivery_at=timezone.now())
        if status.startswith("failed"):
            logger.warning("Webhook %s delivery failed: %s", webhook_pk, status)
    except Exception:
        logger.exception("Failed to record webhook delivery for %s", webhook_pk)
