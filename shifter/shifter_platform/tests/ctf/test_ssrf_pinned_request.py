"""The shared pinned HTTPS request helper (#2090, OWASP SSRF Case 2).

Webhooks and organizer image copies send requests to organizer-chosen URLs. Both
use ``pinned_https_response``: HTTPS only, no URL credentials, every resolved
address public, the socket pinned to a validated address, and redirects returned
as ordinary responses rather than followed.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from ctf.validators import _ssrf


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class _FakeConnection:
    def __init__(self, record: list, *, fail: bool = False, status: int = 200) -> None:
        self.record = record
        self.fail = fail
        self.status = status
        self.closed = False

    def request(self, method, target, *, body, headers):
        if self.fail:
            raise OSError("connect failed")
        self.record.append((method, target, body, headers))

    def getresponse(self):
        return _FakeResponse(self.status)

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "url",
    [
        "http://img.example.test/a.png",
        "https://user:pw@img.example.test/a.png",
        "https://localhost/a.png",
        "https://metadata.google.internal/a.png",
        "not a url",
    ],
)
def test_rejects_unsafe_urls_before_any_connection(monkeypatch, url):
    monkeypatch.setattr(_ssrf, "_build_https_connection", lambda **_kw: pytest.fail("connection attempted"))
    with pytest.raises(_ssrf._BlockedDestinationError), _ssrf.pinned_https_response(url, method="GET", timeout=1):
        pass


def test_rejects_hosts_that_resolve_to_blocked_addresses(monkeypatch):
    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    monkeypatch.setattr(_ssrf, "_build_https_connection", lambda **_kw: pytest.fail("connection attempted"))
    with (
        pytest.raises(_ssrf._BlockedDestinationError),
        _ssrf.pinned_https_response("https://img.example.test/a.png", method="GET", timeout=1),
    ):
        pass


def test_pins_the_validated_address_and_returns_redirects_unfollowed(monkeypatch):
    record: list = []
    connections: list = []

    def build(**kwargs):
        connections.append(kwargs)
        conn = _FakeConnection(record, status=302)
        kwargs["_conn"] = conn
        return conn

    monkeypatch.setattr(_ssrf, "_resolve_and_validate", lambda hostname, port: ["93.184.216.34"])
    monkeypatch.setattr(_ssrf, "_build_https_connection", build)

    with _ssrf.pinned_https_response(
        "https://img.example.test/path/a.png?v=1#frag", method="GET", timeout=3, headers={"Accept": "image/*"}
    ) as response:
        assert response.status == 302

    assert record == [("GET", "/path/a.png?v=1", None, {"Accept": "image/*"})]
    assert connections[0]["hostname"] == "img.example.test"
    assert connections[0]["pinned_ip"] == "93.184.216.34"
    assert connections[0]["timeout"] == 3
    assert connections[0]["_conn"].closed


def test_tries_the_next_validated_address_and_fails_closed_when_none_respond(monkeypatch):
    record: list = []
    attempts: list = []

    def build(**kwargs):
        attempts.append(kwargs["pinned_ip"])
        return _FakeConnection(record, fail=kwargs["pinned_ip"] == "93.184.216.34")

    monkeypatch.setattr(_ssrf, "_resolve_and_validate", lambda hostname, port: ["93.184.216.34", "93.184.216.35"])
    monkeypatch.setattr(_ssrf, "_build_https_connection", build)
    with _ssrf.pinned_https_response("https://img.example.test/a.png", method="GET", timeout=1) as response:
        assert response.status == 200
    assert attempts == ["93.184.216.34", "93.184.216.35"]

    monkeypatch.setattr(_ssrf, "_build_https_connection", lambda **_kw: _FakeConnection([], fail=True))
    with (
        pytest.raises(OSError, match="unavailable"),
        _ssrf.pinned_https_response("https://img.example.test/a.png", method="GET", timeout=1),
    ):
        pass


def test_routes_through_the_configured_egress_proxy_with_end_to_end_tls(monkeypatch, settings):
    """In a deployment the platform pods have no public egress (#2090)."""
    settings.CTF_EGRESS_PROXY = "content-egress:3128"
    record: list = []
    tunnels: list = []

    def build_tunnel(**kwargs):
        tunnels.append(kwargs)
        return _FakeConnection(record)

    monkeypatch.setattr(_ssrf, "_resolve_and_validate", lambda hostname, port: ["93.184.216.34"])
    monkeypatch.setattr(_ssrf, "_build_https_connection", lambda **_kw: pytest.fail("direct connection attempted"))
    monkeypatch.setattr(_ssrf, "_build_tunnel_connection", build_tunnel)

    with _ssrf.pinned_https_response("https://img.example.test/a.png", method="GET", timeout=4) as response:
        assert response.status == 200

    assert len(tunnels) == 1
    assert tunnels[0]["proxy_host"] == "content-egress"
    assert tunnels[0]["proxy_port"] == 3128
    assert tunnels[0]["hostname"] == "img.example.test"
    assert tunnels[0]["port"] == 443
    assert tunnels[0]["timeout"] == 4
    assert record == [("GET", "/a.png", None, {})]


def test_proxy_mode_keeps_the_destination_checks(monkeypatch, settings):
    settings.CTF_EGRESS_PROXY = "content-egress:3128"
    monkeypatch.setattr(_ssrf.socket, "getaddrinfo", lambda *_a, **_k: [(2, 1, 6, "", ("10.0.0.5", 443))])
    monkeypatch.setattr(_ssrf, "_build_tunnel_connection", lambda **_kw: pytest.fail("tunnel attempted"))
    with (
        pytest.raises(_ssrf._BlockedDestinationError),
        _ssrf.pinned_https_response("https://img.example.test/a.png", method="GET", timeout=1),
    ):
        pass


def test_tunnel_connection_targets_the_proxy_and_verifies_the_destination():
    import ssl

    context = ssl.create_default_context()
    connection = _ssrf._build_tunnel_connection(
        proxy_host="content-egress", proxy_port=3128, hostname="img.example.test", port=443, timeout=2, context=context
    )
    assert (connection.host, connection.port) == ("content-egress", 3128)
    assert (connection._tunnel_host, connection._tunnel_port) == ("img.example.test", 443)
    assert connection._context.check_hostname


_URL = "https://img.example.test/a.png"
_COMPLETE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"


def _read_request(sock: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return
        data += chunk


def _serve(sock: socket.socket, mode: str, stop: threading.Event) -> None:
    """Act as the peer: answer promptly, stay silent, or send one byte at a time."""
    try:
        _read_request(sock)
        if mode == "tunnel":
            sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _read_request(sock)
        if mode in {"complete", "tunnel"}:
            sock.sendall(_COMPLETE)
            return
        if mode == "headers":
            sock.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
        else:
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n")
        while not stop.wait(0.02):
            if mode != "silent":
                sock.sendall(b"a")
    except OSError:
        return


class _PairConnection(http.client.HTTPConnection):
    """A plain connection over one end of a socket pair."""

    def __init__(self, sock: socket.socket, *, tunnel: bool) -> None:
        super().__init__("img.example.test", timeout=5)
        self._pair = sock
        if tunnel:
            self.set_tunnel("img.example.test", 443)

    def connect(self) -> None:
        self._pair.settimeout(self.timeout)
        self.sock = self._pair
        if self._tunnel_host:
            self._tunnel()


@contextmanager
def _peer(monkeypatch, mode: str):
    client, server = socket.socketpair()
    stop = threading.Event()
    thread = threading.Thread(target=_serve, args=(server, mode, stop), daemon=True)
    thread.start()
    monkeypatch.setattr(_ssrf, "_resolve_and_validate", lambda hostname, port: ["93.184.216.34"])
    monkeypatch.setattr(
        _ssrf, "_build_https_connection", lambda **_kw: _PairConnection(client, tunnel=mode == "tunnel")
    )
    try:
        yield
    finally:
        stop.set()
        server.close()
        client.close()
        thread.join(timeout=5)


@pytest.mark.parametrize("mode", ["body", "silent", "headers"])
def test_a_deadline_ends_every_receive_of_a_slow_response(monkeypatch, mode):
    """One buffered read cannot outlive the deadline, even when bytes keep arriving (core-F1, #2090)."""
    with _peer(monkeypatch, mode):
        started = time.monotonic()
        with (
            pytest.raises(TimeoutError),
            _ssrf.pinned_https_response(_URL, method="GET", timeout=5, deadline=started + 0.3) as response,
        ):
            response.read(65536)
        # Without the deadline, the body takes 2000 s and a silent peer 5 s per receive.
        assert time.monotonic() - started < 2


@pytest.mark.parametrize("mode", ["complete", "tunnel"])
def test_a_deadline_keeps_a_prompt_response_readable(monkeypatch, mode):
    """The bounded reader keeps the socket open after the connection hands it to the response."""
    with (
        _peer(monkeypatch, mode),
        _ssrf.pinned_https_response(_URL, method="GET", timeout=5, deadline=time.monotonic() + 5) as response,
    ):
        assert response.read() == b"hello"
