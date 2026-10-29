"""SSRF / DNS-pinning policy for programmable and HTTP flag validators.

Owns the blocklist policy (private, loopback, link-local, reserved,
multicast, unspecified addresses plus the metadata/localhost hostname
list), DNS resolution against that policy, and the pinned-IP HTTPS
connection used to close the resolve-then-connect TOCTOU window.
"""

from __future__ import annotations

import http.client
import io
import ipaddress
import socket
import ssl
import time
from collections.abc import Buffer, Iterator
from contextlib import contextmanager, suppress
from typing import Any
from urllib.parse import urlparse

# Blocked hostnames for SSRF protection
_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
    }
)


class _BlockedDestinationError(Exception):
    """Raised when DNS resolution returns a blocked address.

    Used by the HTTP validator path so the caller can distinguish
    "every answer in the DNS reply violates SSRF policy" from a
    "resolution failed" outcome and fail closed in either case.
    """


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *addr* is in a range we never allow as a network target.

    Only globally routable unicast addresses are admitted, the same non-global
    rule the content egress proxy applies. ``is_global`` is false for private,
    loopback, link-local, reserved, and unspecified addresses, and also for
    special-purpose ranges that ``is_private`` misses, such as the RFC 6598
    shared address space (100.64.0.0/10) (#2617). Some multicast ranges are
    global, so multicast is refused explicitly.
    """
    return bool(not addr.is_global or addr.is_multicast)


def _resolve_and_validate(hostname: str, port: int) -> list[str]:
    """Resolve *hostname* and apply SSRF policy to every address returned.

    Returns the list of resolved address strings (all of which passed
    policy). Raises ``_BlockedDestinationError`` if any address in the
    DNS reply is blocked, or if the reply is empty. ``socket.gaierror``
    propagates unchanged for callers that want to distinguish lookup
    failure.

    The address list returned here is the input to
    ``_PinnedHTTPSConnection``: the actual socket must be opened to one
    of these addresses, never re-resolved, so a hostname that flips to
    a blocked address between this call and the connect step cannot
    reach the wire.
    """
    infos = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    addresses: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        addr_str = str(sockaddr[0])
        try:
            addr_obj = ipaddress.ip_address(addr_str)
        except ValueError as exc:
            raise _BlockedDestinationError(f"non-IP address in DNS response: {addr_str!r}") from exc
        if _is_blocked_address(addr_obj):
            raise _BlockedDestinationError(f"blocked address from DNS for {hostname!r}")
        addresses.append(addr_str)
    if not addresses:
        raise _BlockedDestinationError(f"DNS returned no addresses for {hostname!r}")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a pre-validated IP.

    The TCP socket is opened to ``pinned_ip``; TLS uses the original
    ``hostname`` for SNI and certificate verification; ``Host:`` keeps
    the original hostname (set by the stdlib from ``self.host``).
    Coupling DNS policy to the actual connection target closes the
    rebinding window between policy check and connect.
    """

    def __init__(
        self,
        hostname: str,
        pinned_ip: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host=hostname, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(
        self,
    ) -> None:  # pragma: no cover  # NOSONAR
        sock = socket.create_connection(
            (self._pinned_ip, self.port),
            timeout=self.timeout,
        )
        try:
            if self._tunnel_host:  # type: ignore[attr-defined]
                self.sock = sock
                self._tunnel()  # type: ignore[attr-defined]
            self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
                sock, server_hostname=self.host
            )
        except Exception:
            sock.close()
            raise


class _DeadlineReader(io.RawIOBase):
    """Socket reader that ends every receive by one elapsed-time deadline.

    A socket timeout bounds one receive, so a peer that sends a byte before
    each timeout can keep a buffered read open without limit. Before each
    receive, this reader sets the socket timeout to the time that remains.
    """

    def __init__(self, sock: socket.socket, raw: io.RawIOBase, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._raw = raw
        self._deadline = deadline
        self._timeout = sock.gettimeout()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int | None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline exceeded")
        self._sock.settimeout(remaining if self._timeout is None else min(self._timeout, remaining))
        return self._raw.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


def _deadline_response_class(deadline: float) -> type[http.client.HTTPResponse]:
    """Return an ``HTTPResponse`` class whose every receive ends by ``deadline``."""

    class _DeadlineResponse(http.client.HTTPResponse):
        """An ``HTTPResponse`` that reads through a deadline-bounded socket file."""

        def __init__(self, sock: socket.socket, *args: Any, **kwargs: Any) -> None:
            super().__init__(sock, *args, **kwargs)
            # Keep the stock socket file (and its socket reference); bound its receives.
            self.fp = io.BufferedReader(_DeadlineReader(sock, self.fp.detach(), deadline))

    return _DeadlineResponse


def _build_https_connection(
    *,
    hostname: str,
    pinned_ip: str,
    port: int,
    timeout: float,
    context: ssl.SSLContext,
) -> _PinnedHTTPSConnection:
    """Factory seam — kept module-level so tests can patch it cleanly."""
    return _PinnedHTTPSConnection(
        hostname=hostname,
        pinned_ip=pinned_ip,
        port=port,
        timeout=timeout,
        context=context,
    )


def _build_tunnel_connection(
    *,
    proxy_host: str,
    proxy_port: int,
    hostname: str,
    port: int,
    timeout: float,
    context: ssl.SSLContext,
) -> http.client.HTTPSConnection:
    """Factory seam for a CONNECT tunnel through the platform egress proxy.

    TLS is negotiated inside the tunnel with ``hostname`` for SNI and certificate
    verification, so the proxy relays opaque bytes and never sees the request.
    """
    connection = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=timeout, context=context)
    connection.set_tunnel(hostname, port)
    return connection


def _egress_proxy() -> tuple[str, int] | None:
    """Return the configured ``(host, port)`` of the platform egress proxy, if any."""
    from django.conf import settings

    value = getattr(settings, "CTF_EGRESS_PROXY", "")
    if not value:
        return None
    host, _, port = value.rpartition(":")
    return host, int(port)


def _safe_parse_url(url: str) -> tuple[Any, str, int] | None:
    """Return ``(parsed, hostname, port)`` for *url*, or None if malformed.

    Folds ``urlparse`` failures, missing hostnames, and invalid or
    out-of-range ports (``parsed.port`` raises ``ValueError`` for inputs
    such as ``https://example.com:bad/`` or ``https://example.com:99999/``)
    into a single ``None`` verdict so the caller can treat the URL as
    unsafe in one place. Centralizing this here closes the class of bug
    where downstream code accesses ``parsed.port`` outside the
    ``urlparse`` try/except envelope and leaks a ``ValueError`` to its
    own caller.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        port = parsed.port if parsed.port is not None else 443
    except (ValueError, TypeError):
        return None
    if not hostname:
        return None
    return parsed, hostname, port


def _hostname_and_port_from_url(url: str) -> tuple[str, int] | None:
    """Return ``(hostname, port)`` for *url*, or None if malformed or directly blocklisted."""
    parsed_tuple = _safe_parse_url(url)
    if parsed_tuple is None:
        return None
    _parsed, hostname, port = parsed_tuple
    if hostname in _BLOCKED_HOSTNAMES:
        return None
    return hostname, port


def _is_blocked_hostname_via_dns(hostname: str, port: int) -> bool:
    """Return True if every address DNS returns for *hostname* is policy-blocked.

    Config-time DNS-lookup failures are tolerated (return False) rather than
    rejecting a legitimate URL whose DNS is briefly unavailable at
    organizer-edit time; the runtime validator re-resolves with pinning and
    fails closed there if the destination is actually unsafe.
    """
    try:
        _resolve_and_validate(hostname, port)
    except _BlockedDestinationError:
        return True
    except OSError:
        # socket.gaierror is a subclass of OSError, so this also covers
        # DNS resolution failure.
        pass
    return False


def is_blocked_url(url: str) -> bool:
    """Return True if *url* targets a blocked or private network address.

    Applied at flag-creation time (``_validate_http_config``) so organizers
    cannot persist an obviously unsafe destination. The runtime validator
    re-resolves with pinning, so config-time DNS-lookup failures here are
    tolerated (return False) rather than rejecting a legitimate URL whose
    DNS is briefly unavailable at organizer-edit time.
    """
    resolved = _hostname_and_port_from_url(url)
    if resolved is None:
        return True
    hostname, port = resolved

    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return _is_blocked_hostname_via_dns(hostname, port)

    return _is_blocked_address(addr)


def _connections(
    hostname: str, port: int, pinned_ips: list[str], *, timeout: float, context: ssl.SSLContext
) -> Iterator[http.client.HTTPSConnection]:
    """Yield the connections to try: one proxy tunnel, or each validated address."""
    proxy = _egress_proxy()
    if proxy is not None:
        # Deployed pods have no public egress; the proxy re-checks and pins the address.
        yield _build_tunnel_connection(
            proxy_host=proxy[0], proxy_port=proxy[1], hostname=hostname, port=port, timeout=timeout, context=context
        )
        return
    for pinned_ip in pinned_ips:
        yield _build_https_connection(
            hostname=hostname, pinned_ip=pinned_ip, port=port, timeout=timeout, context=context
        )


def _validated_destination(url: str) -> tuple[Any, str, int]:
    """Return ``(parsed, hostname, port)`` for an admissible HTTPS URL, or raise."""
    parsed_tuple = _safe_parse_url(url)
    if parsed_tuple is None:
        raise _BlockedDestinationError("Invalid destination")
    parsed, hostname, port = parsed_tuple
    if parsed.scheme != "https" or parsed.username or parsed.password or hostname in _BLOCKED_HOSTNAMES:
        raise _BlockedDestinationError("Invalid destination")
    return parsed, hostname, port


def _send(
    connection: http.client.HTTPConnection,
    method: str,
    target: str,
    *,
    body: bytes | None,
    headers: dict[str, str],
    deadline: float | None,
) -> http.client.HTTPResponse | None:
    """Send the request on one connection; return ``None`` to try the next address."""
    if deadline is not None:
        connection.response_class = _deadline_response_class(deadline)
    try:
        connection.request(method, target, body=body, headers=headers)
        return connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("request deadline exceeded") from exc
        return None


@contextmanager
def pinned_https_response(
    url: str,
    *,
    method: str,
    timeout: float,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    deadline: float | None = None,
) -> Iterator[http.client.HTTPResponse]:
    """Send one request to an organizer-chosen URL and yield its response.

    The single SSRF-guarded request path for webhooks and organizer image copies
    (OWASP SSRF Case 2): HTTPS only, no URL credentials, every resolved address
    public, the socket pinned to a validated address, and a redirect returned as
    an ordinary response rather than followed. When ``CTF_EGRESS_PROXY`` is set
    (deployed pods have no public egress), the request tunnels through the
    platform egress proxy, which repeats the address check and pins the address. Raises ``_BlockedDestinationError``
    for an unsafe URL and ``OSError`` when no validated address responds. The
    connection closes when the context exits.

    With ``deadline`` (a ``time.monotonic()`` value), every receive of the
    response, including its status line and headers, ends by the deadline, and
    a late read raises ``TimeoutError``.
    """
    from ctf.validators._http import _request_target

    parsed, hostname, port = _validated_destination(url)
    pinned_ips = _resolve_and_validate(hostname, port)
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    target = _request_target(parsed)
    for connection in _connections(hostname, port, pinned_ips, timeout=timeout, context=context):
        try:
            response = _send(connection, method, target, body=body, headers=headers or {}, deadline=deadline)
            if response is not None:
                yield response
                return
        finally:
            with suppress(Exception):
                connection.close()
    raise OSError("Destination transport unavailable")
