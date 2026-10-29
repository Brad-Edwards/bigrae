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
import socket
import ssl
import time
from collections.abc import Buffer, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parseaddr
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import requests
import urllib3
from requests.adapters import HTTPAdapter

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest, AWSResponse
    from django.core.mail import EmailMessage
    from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)

# Bound for request-path sends (invitation, password reset): the HTTP response
# waits for at most one attempt of this length.
INTERACTIVE_TIMEOUT_SECONDS = 10

_SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
_SES_BACKEND = "django_ses.SESBackend"
_ANYMAIL_PREFIX = "anymail.backends."

# A provider receipt is opaque correlation, never provider text.
_RECEIPT_RE = re.compile(r"[\x21-\x7e]{1,255}")


class EmailOutcome(StrEnum):
    """Closed classification of one send attempt.

    ``ACCEPTED`` means the backend took the message; it is not mailbox delivery
    or a read. ``RETRIABLE`` is a transient or deployment fault. ``TERMINAL`` is a
    rejection of this recipient or message. ``UNAVAILABLE`` means no delivering
    backend is configured, so nothing was sent.
    """

    ACCEPTED = "accepted"
    RETRIABLE = "retriable"
    TERMINAL = "terminal"
    UNAVAILABLE = "unavailable"


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


# One classification: the outcome and its closed reason.
Classification = tuple["EmailOutcome", str]


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
    deadline = _Deadline(timeout)
    try:
        message = _message(recipient, subject, html_content, text_content, message_key, from_email)
        sent = _send_once(message, timeout, deadline)
    # Every failure is classified into the closed vocabulary; nothing propagates.
    except Exception as exc:
        # Libraries wrap a socket timeout in their own error types; the deadline
        # itself is the authority on whether the attempt ran out of time.
        outcome, reason = (EmailOutcome.RETRIABLE, "transport_timeout") if deadline.expired() else _classify(exc)
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


def _send_once(message: EmailMessage, timeout: float, deadline: _Deadline) -> int:
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


class _DeadlineExceeded(TimeoutError):
    """The attempt used its whole time budget."""


class _Deadline:
    """One wall-clock budget shared by every transport operation of an attempt."""

    def __init__(self, seconds: float) -> None:
        self._expires = time.monotonic() + seconds

    def expired(self) -> bool:
        """Return True once the budget is spent."""
        return time.monotonic() >= self._expires

    def remaining(self) -> float:
        """Return the seconds left, or raise when none are left."""
        left = self._expires - time.monotonic()
        if left <= 0:
            raise _DeadlineExceeded("email attempt deadline exceeded")
        return left


class _DeadlineSocket:
    """Socket proxy that gives each blocking operation only the time left.

    A per-operation socket timeout restarts on every read; this proxy re-arms the
    real socket with the remaining budget before each read or write, so a slow
    or trickling peer cannot hold the attempt past its deadline. Everything else
    is delegated to the wrapped socket.
    """

    def __init__(self, sock: socket.socket, deadline: _Deadline) -> None:
        self._sock = sock
        self._deadline = deadline

    def __getattr__(self, name: str) -> object:
        return getattr(self._sock, name)

    def _armed(self) -> socket.socket:
        self._sock.settimeout(self._deadline.remaining())
        return self._sock

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        return self._armed().recv(bufsize, flags)

    def recv_into(self, buffer: bytearray | memoryview, nbytes: int = 0, flags: int = 0) -> int:
        return self._armed().recv_into(buffer, nbytes, flags)

    def send(self, data: Buffer, flags: int = 0) -> int:
        return self._armed().send(data, flags)

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        self._armed().sendall(data, flags)

    # The stdlib implementation builds a SocketIO over ``self``, so buffered
    # readers (smtplib replies, http.client responses) read through the proxy.
    makefile = socket.socket.makefile


def _bound_socket(sock: socket.socket, deadline: _Deadline) -> socket.socket:
    """Return ``sock`` behind the deadline proxy (typed as the socket it stands for)."""
    return cast(socket.socket, _DeadlineSocket(sock, deadline))


def _deadline_smtp_class(
    base: type[smtplib.SMTP], deadline: _Deadline, opened: list[smtplib.SMTP]
) -> type[smtplib.SMTP]:
    """Return an smtplib connection class whose sockets share ``deadline``.

    Every connection it opens is recorded in ``opened`` so the attempt can close
    a connection that failed part-way through setup.
    """

    class DeadlineSMTP(base):  # type: ignore[valid-type,misc]
        sock: Any

        def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
            opened.append(self)
            return _bound_socket(super()._get_socket(host, port, deadline.remaining()), deadline)

        def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
            # STARTTLS replaces the socket; keep the replacement on the deadline.
            reply: tuple[int, bytes] = super().starttls(context=context)
            self.sock = _bound_socket(self.sock, deadline)
            return reply

    return DeadlineSMTP


def _deadline_smtp_backend(deadline: _Deadline, opened: list[smtplib.SMTP]) -> BaseEmailBackend:
    """Return Django's SMTP backend with deadline-bound connections."""
    from django.core.mail.backends.smtp import EmailBackend

    class DeadlineSMTPBackend(EmailBackend):
        @property
        def connection_class(self) -> type[smtplib.SMTP]:
            return _deadline_smtp_class(super().connection_class, deadline, opened)

    return DeadlineSMTPBackend(fail_silently=False)


def _deadline_pool_classes(deadline: _Deadline) -> dict[str, type[urllib3.HTTPConnectionPool]]:
    """Return urllib3 pool classes whose connections share ``deadline`` after setup."""

    def bound(base: type[Any]) -> type[Any]:
        class DeadlineConnection(base):
            sock: Any

            def connect(self) -> None:
                super().connect()
                self.sock = _bound_socket(self.sock, deadline)

        return DeadlineConnection

    http_connection = bound(urllib3.connection.HTTPConnection)
    https_connection = bound(urllib3.connection.HTTPSConnection)
    return {
        "http": type("DeadlineHTTPConnectionPool", (urllib3.HTTPConnectionPool,), {"ConnectionCls": http_connection}),
        "https": type(
            "DeadlineHTTPSConnectionPool", (urllib3.HTTPSConnectionPool,), {"ConnectionCls": https_connection}
        ),
    }


class _DeadlineAdapter(HTTPAdapter):
    """requests adapter: no retries, setup within the remaining time, I/O on the deadline."""

    def __init__(self, deadline: _Deadline) -> None:
        self._deadline = deadline
        self._pools = _deadline_pool_classes(deadline)
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: object) -> None:
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = self._pools

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: object) -> urllib3.PoolManager:
        manager: urllib3.PoolManager = super().proxy_manager_for(proxy, **proxy_kwargs)
        manager.pool_classes_by_scheme = self._pools
        return manager

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: None | float | tuple[float, float] | tuple[float, None] = None,
        verify: bool | str = True,
        cert: None | bytes | str | tuple[bytes | str, bytes | str] = None,
        proxies: Mapping[str, str] | None = None,
    ) -> requests.Response:
        remaining = self._deadline.remaining()
        return super().send(
            request, stream=stream, timeout=(remaining, remaining), verify=verify, cert=cert, proxies=proxies
        )


def _deadline_session(deadline: _Deadline, session: requests.Session | None = None) -> requests.Session:
    """Mount the deadline adapter on ``session`` (or a new one) for both schemes."""
    session = session or requests.Session()
    adapter = _DeadlineAdapter(deadline)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _send_signed_request(session: requests.Session, request: AWSPreparedRequest, **_kwargs: object) -> AWSResponse:
    """botocore ``before-send`` hook: send the signed request through the deadline session.

    botocore uses the first non-None response a ``before-send`` handler returns,
    so the SES client keeps its signing, parsing, and error handling while the
    HTTP exchange shares the attempt deadline.
    """
    from botocore.awsrequest import AWSResponse

    prepared = requests.Request(
        request.method, request.url, headers=dict(request.headers.items()), data=request.body
    ).prepare()
    response = session.send(prepared, stream=True)
    return AWSResponse(response.url, response.status_code, response.headers, response.raw)


@contextmanager
def _bounded_connection(backend: str, timeout: float, deadline: _Deadline) -> Iterator[BaseEmailBackend]:
    """Yield a backend connection whose network I/O stops at ``deadline``.

    SMTP, the requests-based Anymail ESPs (SendGrid, Mailgun), and SES get the
    shared deadline. Local development and test backends do no network I/O.
    """
    from django.core.mail import get_connection

    if backend == _SMTP_BACKEND:
        opened: list[smtplib.SMTP] = []
        try:
            yield _deadline_smtp_backend(deadline, opened)
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
            session = connection.session = _deadline_session(deadline, connection.create_session())
        elif backend == _SES_BACKEND:
            connection.open()
            session = _deadline_session(deadline)
            connection.connection.meta.events.register("before-send", partial(_send_signed_request, session))
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


def _first_match(rules: Iterable[tuple[bool, Classification]]) -> Classification | None:
    """Return the classification of the first rule whose condition holds."""
    return next((classification for matched, classification in rules if matched), None)


_RETRY_PROVIDER: Classification = (EmailOutcome.RETRIABLE, "provider_unavailable")
_RETRY_DEPLOYMENT: Classification = (EmailOutcome.RETRIABLE, "transport_unavailable")
_RETRY_THROTTLED: Classification = (EmailOutcome.RETRIABLE, "provider_throttled")
_TERMINAL_PROVIDER: Classification = (EmailOutcome.TERMINAL, "provider_rejected")
_TERMINAL_RECIPIENT: Classification = (EmailOutcome.TERMINAL, "recipient_rejected")
_TERMINAL_MESSAGE: Classification = (EmailOutcome.TERMINAL, "invalid_message")


def _classify(exc: BaseException) -> Classification:
    """Map a backend exception to a closed outcome and reason.

    Deployment faults (credentials, sender identity, missing configuration) are
    retriable: they say nothing about the recipient. Only a rejection of this
    recipient or this message is terminal.
    """
    classified = (classify(exc) for classify in (_classify_anymail, _classify_ses, _classify_smtp, _classify_generic))
    return next((result for result in classified if result is not None), (EmailOutcome.RETRIABLE, "transport_error"))


def _classify_http_status(status_code: int | None) -> Classification:
    """Classify an ESP HTTP status code."""
    client_error = status_code is not None and 400 <= status_code < 500 and status_code != 408
    rules = [
        (status_code in (401, 403, 404), _RETRY_DEPLOYMENT),
        (status_code == 429, _RETRY_THROTTLED),
        (client_error, _TERMINAL_PROVIDER),
    ]
    return _first_match(rules) or _RETRY_PROVIDER


def _classify_anymail(exc: BaseException) -> Classification | None:
    """Classify django-anymail exceptions (GCP SendGrid/Mailgun path)."""
    from anymail.exceptions import (
        AnymailAPIError,
        AnymailConfigurationError,
        AnymailError,
        AnymailInvalidAddress,
        AnymailRecipientsRefused,
    )

    api_error = isinstance(exc, AnymailAPIError)
    return _first_match(
        [
            (isinstance(exc, AnymailRecipientsRefused), _TERMINAL_RECIPIENT),
            (isinstance(exc, AnymailInvalidAddress), _TERMINAL_MESSAGE),
            (api_error, _classify_http_status(getattr(exc, "status_code", None)) if api_error else _RETRY_PROVIDER),
            (isinstance(exc, AnymailConfigurationError), _RETRY_DEPLOYMENT),
            (isinstance(exc, AnymailError), _TERMINAL_MESSAGE),
        ]
    )


def _classify_ses(exc: BaseException) -> Classification | None:
    """Classify botocore exceptions (AWS django-ses path)."""
    from botocore.exceptions import BotoCoreError, ClientError

    code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""
    return _first_match(
        [
            (code == "MessageRejected", _TERMINAL_PROVIDER),
            (code in ("Throttling", "ThrottlingException", "TooManyRequestsException"), _RETRY_THROTTLED),
            (isinstance(exc, ClientError | BotoCoreError), _RETRY_PROVIDER),
        ]
    )


def _classify_smtp(exc: BaseException) -> Classification | None:
    """Classify smtplib exceptions (SMTP backend path).

    A 4xx RCPT reply (greylisting, mailbox busy) is temporary; a refusal is
    permanent only when every refused recipient got a 5xx reply.
    """
    refused = isinstance(exc, smtplib.SMTPRecipientsRefused)
    codes = [reply[0] for reply in exc.recipients.values()] if isinstance(exc, smtplib.SMTPRecipientsRefused) else []
    permanent_refusal = bool(codes) and all(isinstance(code, int) and code >= 500 for code in codes)
    permanent_reply = isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code >= 500
    return _first_match(
        [
            (refused and permanent_refusal, _TERMINAL_RECIPIENT),
            (refused, (EmailOutcome.RETRIABLE, "recipient_deferred")),
            (isinstance(exc, smtplib.SMTPAuthenticationError | smtplib.SMTPSenderRefused), _RETRY_DEPLOYMENT),
            (isinstance(exc, smtplib.SMTPConnectError), _RETRY_PROVIDER),
            (permanent_reply, _TERMINAL_PROVIDER),
            (isinstance(exc, smtplib.SMTPException), _RETRY_PROVIDER),
        ]
    )


def _classify_generic(exc: BaseException) -> Classification | None:
    """Classify network, configuration, and message-construction errors."""
    from django.core.exceptions import ImproperlyConfigured

    return _first_match(
        [
            (isinstance(exc, OSError), _RETRY_PROVIDER),
            (isinstance(exc, ImproperlyConfigured), _RETRY_DEPLOYMENT),
            # Django header/address validation (BadHeaderError is ValueError).
            (isinstance(exc, ValueError), _TERMINAL_MESSAGE),
        ]
    )
