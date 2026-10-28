"""Exercise the native Guacamole session DELETE over a local HTTP boundary."""

import json
import logging
import time
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from mission_control.guacamole import GuacamoleClientConfig, JsonAuthGuacamoleClient

_TOKEN = "SYNTHETIC_NATIVE_GUAC_TOKEN"


@contextmanager
def server(status=204, *, delay=0, body=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_DELETE(self):
            received.append((self.path, self.headers.get("Guacamole-Token")))
            time.sleep(delay)
            self.send_response(status)
            if status == 303:
                self.send_header("Location", "/unexpected")
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body is not None:
                self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            received.append((self.path, self.headers.get("Guacamole-Token")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/guacamole", received
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def client(url, timeout=0.2):
    return JsonAuthGuacamoleClient(
        GuacamoleClientConfig(
            base_url="https://example.test/guacamole", api_base_url=url, secret_key="0" * 32, timeout_seconds=timeout
        )
    )


def test_session_delete_uses_internal_origin_and_token_header():
    with server() as (url, received):
        assert client(url).terminate_session(_TOKEN)
    assert received == [("/guacamole/api/session", _TOKEN)]


@pytest.mark.parametrize("status", [303, 403, 404, 500])
def test_failed_or_redirected_delete_does_not_claim_termination(status):
    with server(status) as (url, received):
        assert not client(url).terminate_session(_TOKEN)
    assert received == [("/guacamole/api/session", _TOKEN)]


def test_unresponsive_delete_is_bounded():
    with server(delay=0.5) as (url, _):
        started = time.monotonic()
        assert not client(url, timeout=0.05).terminate_session(_TOKEN)
        assert time.monotonic() - started < 0.3


def test_native_absence_response_finishes_repeated_termination():
    with server(403, body={"type": "PERMISSION_DENIED", "message": "Permission Denied."}) as (url, received):
        assert client(url).terminate_session(_TOKEN)
    assert received == [("/guacamole/api/session", _TOKEN)]


@pytest.mark.parametrize(
    "body", [None, {}, {"type": "INTERNAL_ERROR"}, {"type": "PERMISSION_DENIED", "message": "proxy"}]
)
def test_generic_gateway_denial_does_not_confirm_absence(body):
    with server(403, body=body) as (url, _):
        assert not client(url).terminate_session(_TOKEN)


def test_provider_failure_does_not_echo_secret_to_errors_or_logs(monkeypatch, caplog):
    import mission_control.guacamole as guac

    def failed(*args, **kwargs):
        raise urllib.error.URLError(_TOKEN)

    monkeypatch.setattr(guac.urllib.request, "urlopen", failed)
    with caplog.at_level(logging.WARNING, logger=guac.__name__), pytest.raises(ValueError) as caught:
        client("http://127.0.0.1", timeout=0.05)._get_auth_token(_TOKEN)
    assert _TOKEN not in str(caught.value)
    assert _TOKEN not in caplog.text
