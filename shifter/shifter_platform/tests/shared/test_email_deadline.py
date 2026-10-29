"""The whole email attempt stops at one deadline (ADR-071, #1525 review core-F1).

Each test drives the real Django SMTP backend, the real Anymail SendGrid backend,
or the real django-ses backend against a local TCP server. A slow server keeps
every single operation under the per-operation timeout, so only a whole-attempt
deadline can stop it. Only the external peer is simulated (ADR-019-R1).
"""

from __future__ import annotations

import contextlib
import socketserver
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from shared import email

pytestmark = pytest.mark.django_db
BUDGET = 1.0
# Generous slack for slow CI hosts; a per-operation timeout would run ~3x longer.
UPPER_BOUND = BUDGET + 0.9


class _Handler(socketserver.StreamRequestHandler):
    behaviour: Callable[[socketserver.StreamRequestHandler], None]

    def handle(self) -> None:
        # The client may drop the connection at its deadline; that is expected.
        with contextlib.suppress(OSError):
            type(self).behaviour(self)


@pytest.fixture
def serve() -> Iterator[Callable[[Callable[[socketserver.StreamRequestHandler], None]], int]]:
    servers: list[socketserver.ThreadingTCPServer] = []

    def start(behaviour: Callable[[socketserver.StreamRequestHandler], None]) -> int:
        handler = type("Handler", (_Handler,), {"behaviour": staticmethod(behaviour)})
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _smtp(delay: float) -> Callable[[socketserver.StreamRequestHandler], None]:
    """A minimal SMTP peer that waits ``delay`` seconds before every reply."""

    def behaviour(peer: socketserver.StreamRequestHandler) -> None:
        peer.wfile.write(b"220 local ESMTP\r\n")
        while line := peer.rfile.readline():
            command = line.strip().upper()
            time.sleep(delay)
            if command.startswith((b"EHLO", b"HELO")):
                peer.wfile.write(b"250 local\r\n")
            elif command == b"DATA":
                peer.wfile.write(b"354 go ahead\r\n")
                while peer.rfile.readline() not in (b".\r\n", b""):
                    pass
                time.sleep(delay)
                peer.wfile.write(b"250 queued\r\n")
            elif command == b"QUIT":
                peer.wfile.write(b"221 bye\r\n")
                return
            else:
                peer.wfile.write(b"250 ok\r\n")

    return behaviour


def _http(
    status: bytes, headers: bytes, body: bytes, byte_delay: float
) -> Callable[[socketserver.StreamRequestHandler], None]:
    """An HTTP peer that answers once, trickling the body ``byte_delay`` apart."""

    def behaviour(peer: socketserver.StreamRequestHandler) -> None:
        length = 0
        while (line := peer.rfile.readline()) not in (b"\r\n", b""):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        peer.rfile.read(length)
        peer.wfile.write(b"HTTP/1.1 " + status + b"\r\n" + headers + b"Content-Length: %d\r\n\r\n" % len(body))
        for index in range(len(body)):
            peer.wfile.write(body[index : index + 1])
            peer.wfile.flush()
            time.sleep(byte_delay)

    return behaviour


def _timed_attempt() -> tuple[email.EmailAttempt, float]:
    started = time.monotonic()
    result = email.attempt_email("person@example.test", "Subject", "<p>Body</p>", "Body", timeout=BUDGET)
    return result, time.monotonic() - started


@pytest.fixture
def smtp_backend(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST = "127.0.0.1"
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_USE_SSL = False
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""
    return settings


def test_smtp_exchange_is_accepted_through_the_deadline_socket(smtp_backend, serve):
    smtp_backend.EMAIL_PORT = serve(_smtp(delay=0.0))

    result, _elapsed = _timed_attempt()

    assert result.outcome == email.EmailOutcome.ACCEPTED


def test_delayed_smtp_replies_stop_at_the_whole_attempt_deadline(smtp_backend, serve):
    # Each reply (0.35 s) fits the per-operation timeout; six replies do not fit the budget.
    smtp_backend.EMAIL_PORT = serve(_smtp(delay=0.35))

    result, elapsed = _timed_attempt()

    assert (result.outcome, result.reason) == (email.EmailOutcome.RETRIABLE, "transport_timeout")
    assert elapsed < UPPER_BOUND


@pytest.fixture
def sendgrid_backend(settings):
    def configure(port: int) -> None:
        settings.EMAIL_BACKEND = "anymail.backends.sendgrid.EmailBackend"
        settings.ANYMAIL = {"SENDGRID_API_KEY": "test-key", "SENDGRID_API_URL": f"http://127.0.0.1:{port}/v3/"}

    return configure


def test_sendgrid_response_is_accepted_with_its_receipt(sendgrid_backend, serve):
    sendgrid_backend(serve(_http(b"202 Accepted", b"", b"", 0.0)))

    result, _elapsed = _timed_attempt()

    # Anymail assigns the SendGrid message id it sends; it becomes the receipt.
    assert result.outcome == email.EmailOutcome.ACCEPTED
    assert result.receipt


def test_trickled_sendgrid_response_stops_at_the_whole_attempt_deadline(sendgrid_backend, serve):
    # Every byte arrives well inside the per-read timeout; the body never finishes in time.
    sendgrid_backend(serve(_http(b"202 Accepted", b"Content-Type: application/json\r\n", b"{" + b" " * 40 + b"}", 0.1)))

    result, elapsed = _timed_attempt()

    assert (result.outcome, result.reason) == (email.EmailOutcome.RETRIABLE, "transport_timeout")
    assert elapsed < UPPER_BOUND


_SES_OK = (
    b'<SendRawEmailResponse xmlns="http://ses.amazonaws.com/doc/2010-12-01/">'
    b"<SendRawEmailResult><MessageId>ses-receipt-1</MessageId></SendRawEmailResult>"
    b"<ResponseMetadata><RequestId>req-1</RequestId></ResponseMetadata></SendRawEmailResponse>"
)


@pytest.fixture
def ses_backend(settings, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def configure(port: int) -> None:
        settings.EMAIL_BACKEND = "django_ses.SESBackend"
        settings.AWS_SES_REGION_ENDPOINT_URL = f"http://127.0.0.1:{port}"

    return configure


def test_ses_response_is_accepted_through_the_signed_deadline_session(ses_backend, serve):
    ses_backend(serve(_http(b"200 OK", b"Content-Type: text/xml\r\n", _SES_OK, 0.0)))

    result, _elapsed = _timed_attempt()

    assert (result.outcome, result.receipt) == (email.EmailOutcome.ACCEPTED, "ses-receipt-1")


def test_trickled_ses_response_stops_at_the_whole_attempt_deadline(ses_backend, serve):
    ses_backend(serve(_http(b"200 OK", b"Content-Type: text/xml\r\n", _SES_OK, 0.05)))

    result, elapsed = _timed_attempt()

    assert (result.outcome, result.reason) == (email.EmailOutcome.RETRIABLE, "transport_timeout")
    assert elapsed < UPPER_BOUND
