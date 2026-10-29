"""Shared email templating and transport seam (PLAT-103, ADR-071).

Provides platform-wide helpers for rendering Django email templates and making
one bounded, classified send attempt. Other apps import from this module rather
than assembling ``EmailMultiAlternatives`` directly.

Key features
~~~~~~~~~~~~
* ``render_template`` — renders an HTML/text template pair with context
  variable substitution.
* ``attempt_email`` — exactly one backend call within one wall-clock deadline.
  Every blocking read and write of the transport connection gets only the time
  left in the attempt, so the whole send stops at the deadline. It returns a
  closed outcome, a closed reason, and an optional validated provider receipt.
  It never retries; the caller owns retry (the communication worker) or resend
  (interactive invitation/reset messages).
* ``delivery_available`` — whether the deployment has a delivering backend.

Results never carry or log a recipient, a body, provider text, or a traceback.
"""

from __future__ import annotations

import hashlib
import logging
import re
import smtplib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parseaddr
from functools import partial
from typing import TYPE_CHECKING, Any

from shared.email_classification import EmailOutcome, classify
from shared.email_deadline import Deadline, deadline_session, deadline_smtp_backend, send_signed_request

if TYPE_CHECKING:
    import requests
    from django.core.mail import EmailMessage
    from django.core.mail.backends.base import BaseEmailBackend

__all__ = [
    "INTERACTIVE_TIMEOUT_SECONDS",
    "REASONS",
    "EmailAttempt",
    "EmailOutcome",
    "attempt_email",
    "connection_options",
    "delivery_available",
    "render_template",
]

logger = logging.getLogger(__name__)

# Bound for request-path sends (invitation, password reset): the HTTP response
# waits for at most one attempt of this length.
INTERACTIVE_TIMEOUT_SECONDS = 10

_SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
_SES_BACKEND = "django_ses.SESBackend"
_ANYMAIL_PREFIX = "anymail.backends."

# A provider receipt is opaque correlation, never provider text.
_RECEIPT_RE = re.compile(r"[\x21-\x7e]{1,255}")


# The closed reason vocabulary shared by logs, ledger rows, and invitation state.
REASONS = frozenset(
    {
        "backend_accepted",
        "not_accepted",
        "recipient_rejected",
        "invalid_message",
        "provider_rejected",
        "provider_throttled",
        "provider_unavailable",
        "recipient_deferred",
        "transport_timeout",
        "transport_unavailable",
        "transport_error",
    }
)


@dataclass(frozen=True)
class EmailAttempt:
    """The observed result of one send attempt."""

    outcome: EmailOutcome
    reason: str
    receipt: str = ""


def delivery_available() -> bool:
    """Return True when the configured backend can hand mail to a transport."""
    from django.conf import settings

    return bool(getattr(settings, "EMAIL_DELIVERY_AVAILABLE", False))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_template(template_path: str, context: dict[str, Any]) -> tuple[str, str]:
    """Render an HTML + plain-text email template pair.

    Args:
        template_path: Path prefix relative to the templates root **without**
            the extension.  For example ``"ctf/email/invitation"`` will render
            ``ctf/email/invitation.html`` and ``ctf/email/invitation.txt``.
        context: Template context dict for variable substitution.

    Returns:
        ``(html_content, text_content)`` tuple.
    """
    from django.template.loader import render_to_string

    html_content = render_to_string(f"{template_path}.html", context)
    text_content = render_to_string(f"{template_path}.txt", context)
    return html_content, text_content


def connection_options(backend: str, timeout: float) -> dict[str, Any]:
    """Return backend keyword arguments that bound each HTTP operation to ``timeout``.

    These per-operation limits sit under the whole-attempt deadline. HTTP-based
    backends get half of the budget for connect and half for read. SDK retries
    are disabled so the caller stays the only retry owner. The SMTP connection
    class takes its limits from the deadline itself.
    """
    half = timeout / 2
    options: dict[str, Any] = {}
    if backend.startswith(_ANYMAIL_PREFIX):
        options = {"requests_timeout": (half, half)}
    elif backend == _SES_BACKEND:
        from botocore.config import Config

        options = {"aws_config": Config(connect_timeout=half, read_timeout=half, retries={"total_max_attempts": 1})}
    return options


def attempt_email(
    recipient: str,
    subject: str,
    html_content: str,
    text_content: str,
    *,
    timeout: float,
    message_key: str = "",
    from_email: str | None = None,
) -> EmailAttempt:
    """Make exactly one bounded send attempt to one recipient and classify it.

    Args:
        recipient: The single destination address (no shared To/Cc/Bcc list).
        subject: Subject line; it must not contain line breaks.
        html_content: HTML body.
        text_content: Plain-text body.
        timeout: Wall-clock budget, in seconds, for the whole attempt. Each
            blocking operation of the transport connection gets only the time
            left; connection setup uses the remaining time for each step.
        message_key: Stable caller key. When set, the message carries a
            Message-ID derived from it, so every retry of one logical message
            shares that header.
        from_email: Sender address. Falls back to ``DEFAULT_FROM_EMAIL``.
    """
    if not delivery_available():
        return _result(EmailOutcome.UNAVAILABLE, "transport_unavailable")
    deadline = Deadline(timeout)
    try:
        message = _message(recipient, subject, html_content, text_content, message_key, from_email)
        sent = _send_once(message, timeout, deadline)
    # Every failure maps to the closed vocabulary; only its class is logged (#2569).
    except Exception as exc:  # noqa: BLE001  # any backend error is classified, never propagated
        # Libraries wrap a socket timeout in their own error types; the deadline
        # itself is the authority on whether the attempt ran out of time.
        outcome, reason = (EmailOutcome.RETRIABLE, "transport_timeout") if deadline.expired() else classify(exc)
        return _result(outcome, reason, error=type(exc).__name__)
    return _observed(message, sent)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _message(
    recipient: str, subject: str, html_content: str, text_content: str, message_key: str, from_email: str | None
) -> EmailMessage:
    """Build one single-recipient multipart message with an optional stable Message-ID."""
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives

    sender = from_email or getattr(settings, "DEFAULT_FROM_EMAIL", None)
    headers = {"Message-ID": _message_id(message_key, sender or "")} if message_key else None
    message = EmailMultiAlternatives(
        subject=subject, body=text_content, from_email=sender, to=[recipient], headers=headers
    )
    if html_content:
        message.attach_alternative(html_content, "text/html")
    return message


def _send_once(message: EmailMessage, timeout: float, deadline: Deadline) -> int:
    """Hand ``message`` to a deadline-bound connection exactly once."""
    from django.conf import settings

    with _bounded_connection(settings.EMAIL_BACKEND, timeout, deadline) as connection:
        message.connection = connection
        return message.send()


def _observed(message: EmailMessage, sent: int) -> EmailAttempt:
    """Turn the backend's sent count into a result; only a count of one is acceptance."""
    if sent != 1:
        return _result(EmailOutcome.RETRIABLE, "not_accepted")
    return EmailAttempt(EmailOutcome.ACCEPTED, "backend_accepted", _receipt(message))


@contextmanager
def _bounded_connection(backend: str, timeout: float, deadline: Deadline) -> Iterator[BaseEmailBackend]:
    """Yield a backend connection whose network I/O stops at ``deadline``.

    SMTP, the requests-based Anymail ESPs (SendGrid, Mailgun), and SES get the
    shared deadline. Local development and test backends do no network I/O.
    """
    from django.core.mail import get_connection

    if backend == _SMTP_BACKEND:
        opened: list[smtplib.SMTP] = []
        try:
            yield deadline_smtp_backend(deadline, opened)
        finally:
            # Closing is local (no SMTP QUIT): it releases a connection that an
            # expired deadline interrupted during setup.
            for smtp in opened:
                smtp.close()
        return
    connection = get_connection(fail_silently=False, **connection_options(backend, timeout))
    session: requests.Session | None = None
    try:
        if backend.startswith(_ANYMAIL_PREFIX) and hasattr(connection, "create_session"):
            # Anymail keeps a session that already exists and does not close it.
            session = connection.session = deadline_session(deadline, connection.create_session())
        elif backend == _SES_BACKEND:
            connection.open()
            session = deadline_session(deadline)
            connection.connection.meta.events.register("before-send", partial(send_signed_request, session))
        yield connection
    finally:
        if session is not None:
            session.close()
        if backend == _SES_BACKEND:
            connection.close()


def _result(outcome: EmailOutcome, reason: str, *, error: str = "") -> EmailAttempt:
    """Log one bounded line for a non-accepted attempt and return its result."""
    logger.warning("email attempt not accepted: outcome=%s reason=%s error=%s", outcome.value, reason, error or "-")
    return EmailAttempt(outcome, reason)


def _message_id(message_key: str, sender: str) -> str:
    """Build a stable RFC 5322 Message-ID from the caller key and the sender domain."""
    domain = parseaddr(sender)[1].rpartition("@")[2] or "localhost"
    digest = hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:40]
    return f"<{digest}@{domain}>"


def _receipt(message: EmailMessage) -> str:
    """Return the provider message identity when the backend exposes a bounded one."""
    status = getattr(message, "anymail_status", None)
    value = getattr(status, "message_id", None)
    if value is None:
        value = (getattr(message, "extra_headers", None) or {}).get("message_id")
    return value if isinstance(value, str) and _RECEIPT_RE.fullmatch(value) else ""
