"""Whole-attempt deadline for email transports (ADR-071, #1525).

A per-operation socket timeout restarts on every read, so it cannot bound a slow
or trickling peer. These helpers give every blocking read and write of one email
attempt only the time left in a single wall-clock budget: the Django SMTP
connection (including after STARTTLS), the urllib3 connections of a requests
session (the Anymail HTTP ESPs), and SES through a botocore ``before-send`` hook
that sends the signed request over that session. Connection setup uses the
remaining time as its limit for each step; DNS lookup is outside the budget.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
import time
from collections.abc import Buffer, Mapping
from typing import TYPE_CHECKING, Any, cast

import requests
import urllib3
from requests.adapters import HTTPAdapter

if TYPE_CHECKING:
    from botocore.awsrequest import AWSPreparedRequest, AWSResponse
    from django.core.mail.backends.base import BaseEmailBackend


class DeadlineExceeded(TimeoutError):
    """The attempt used its whole time budget."""


class Deadline:
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
            raise DeadlineExceeded("email attempt deadline exceeded")
        return left


class DeadlineSocket:
    """Socket proxy that gives each blocking operation only the time left.

    A per-operation socket timeout restarts on every read; this proxy re-arms the
    real socket with the remaining budget before each read or write, so a slow
    or trickling peer cannot hold the attempt past its deadline. Everything else
    is delegated to the wrapped socket.
    """

    def __init__(self, sock: socket.socket, deadline: Deadline) -> None:
        self._sock = sock
        self._deadline = deadline

    def __getattr__(self, name: str) -> object:
        return getattr(self._sock, name)

    def _armed(self) -> socket.socket:
        self._sock.settimeout(self._deadline.remaining())
        return self._sock

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        """Receive within the time left."""
        return self._armed().recv(bufsize, flags)

    def recv_into(self, buffer: bytearray | memoryview, nbytes: int = 0, flags: int = 0) -> int:
        """Receive into ``buffer`` within the time left."""
        return self._armed().recv_into(buffer, nbytes, flags)

    def send(self, data: Buffer, flags: int = 0) -> int:
        """Send within the time left."""
        return self._armed().send(data, flags)

    def sendall(self, data: Buffer, flags: int = 0) -> None:
        """Send all of ``data`` within the time left."""
        self._armed().sendall(data, flags)

    # The stdlib implementation builds a SocketIO over ``self``, so buffered
    # readers (smtplib replies, http.client responses) read through the proxy.
    makefile = socket.socket.makefile


def bound_socket(sock: socket.socket, deadline: Deadline) -> socket.socket:
    """Return ``sock`` behind the deadline proxy (typed as the socket it stands for)."""
    return cast(socket.socket, DeadlineSocket(sock, deadline))


def deadline_smtp_class(base: type[smtplib.SMTP], deadline: Deadline, opened: list[smtplib.SMTP]) -> type[smtplib.SMTP]:
    """Return an smtplib connection class whose sockets share ``deadline``.

    Every connection it opens is recorded in ``opened`` so the attempt can close
    a connection that failed part-way through setup.
    """

    class DeadlineSMTP(base):  # type: ignore[valid-type,misc]
        """smtplib connection whose sockets share the attempt deadline."""

        sock: socket.socket

        def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
            opened.append(self)
            return bound_socket(super()._get_socket(host, port, deadline.remaining()), deadline)

        def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
            # STARTTLS replaces the socket; keep the replacement on the deadline.
            reply: tuple[int, bytes] = super().starttls(context=context)
            self.sock = bound_socket(self.sock, deadline)
            return reply

    return DeadlineSMTP


def deadline_smtp_backend(deadline: Deadline, opened: list[smtplib.SMTP]) -> BaseEmailBackend:
    """Return Django's SMTP backend with deadline-bound connections."""
    from django.core.mail.backends.smtp import EmailBackend

    class DeadlineSMTPBackend(EmailBackend):
        """Django SMTP backend that opens deadline-bound connections."""

        @property
        def connection_class(self) -> type[smtplib.SMTP]:
            return deadline_smtp_class(super().connection_class, deadline, opened)

    return DeadlineSMTPBackend(fail_silently=False)


def deadline_pool_classes(deadline: Deadline) -> dict[str, type[urllib3.HTTPConnectionPool]]:
    """Return urllib3 pool classes whose connections share ``deadline`` after setup."""

    def bound(base: type[Any]) -> type[Any]:
        """Return ``base`` with its post-setup socket on the deadline."""

        class DeadlineConnection(base):
            """urllib3 connection whose socket shares the attempt deadline after setup."""

            sock: socket.socket

            def connect(self) -> None:
                super().connect()
                self.sock = bound_socket(self.sock, deadline)

        return DeadlineConnection

    http_connection = bound(urllib3.connection.HTTPConnection)
    https_connection = bound(urllib3.connection.HTTPSConnection)
    return {
        "http": type("DeadlineHTTPConnectionPool", (urllib3.HTTPConnectionPool,), {"ConnectionCls": http_connection}),
        "https": type(
            "DeadlineHTTPSConnectionPool", (urllib3.HTTPSConnectionPool,), {"ConnectionCls": https_connection}
        ),
    }


class DeadlineAdapter(HTTPAdapter):
    """requests adapter: no retries, setup within the remaining time, I/O on the deadline."""

    def __init__(self, deadline: Deadline) -> None:
        self._deadline = deadline
        self._pools = deadline_pool_classes(deadline)
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: object) -> None:
        """Create the pool manager with deadline-bound connection pools."""
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = self._pools

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: object) -> urllib3.PoolManager:
        """Return a proxy manager with deadline-bound connection pools."""
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
        """Send with connect and read limited to the time left."""
        remaining = self._deadline.remaining()
        return super().send(
            request, stream=stream, timeout=(remaining, remaining), verify=verify, cert=cert, proxies=proxies
        )


def deadline_session(deadline: Deadline, session: requests.Session | None = None) -> requests.Session:
    """Route every transport of ``session`` (or a new one) through the deadline adapter."""
    session = session or requests.Session()
    adapter = DeadlineAdapter(deadline)
    # Replace every transport the session already routes to, whatever scheme the
    # configured backend uses, so no request leaves outside the deadline.
    for prefix in list(session.adapters):
        session.mount(prefix, adapter)
    return session


def send_signed_request(session: requests.Session, request: AWSPreparedRequest, **_kwargs: object) -> AWSResponse:
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
