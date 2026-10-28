"""Bounded CONNECT-only HTTPS egress relay shared by the platform egress proxies.

Each proxy process supplies two policies: ``select_host`` admits the requested
authority, and ``resolve`` returns the one public address to dial. TLS terminates
at the destination, so the proxy never sees request content or credentials. The
module has no Django, cloud identity, or application dependency.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

MAX_CONNECT_HEADER_BYTES = 4096
_MAX_CONNECTIONS = 256
_SETUP_TIMEOUT_SECONDS = 8
_TUNNEL_LIFETIME_SECONDS = 900
_RELAY_CHUNK_BYTES = 16384
_active = 0

SelectHost = Callable[[bytes], str]
Resolve = Callable[[str], Awaitable[str]]


def parse_connect_authority(header: bytes) -> str:
    """Return the host of a bounded ``CONNECT host:443 HTTP/1.1`` request line."""
    if len(header) > MAX_CONNECT_HEADER_BYTES:
        raise ValueError("invalid CONNECT request")
    line = header.split(b"\r\n", 1)[0].decode("ascii")
    method, authority, version = line.split(" ")
    if method != "CONNECT" or version != "HTTP/1.1" or not authority.endswith(":443"):
        raise ValueError("invalid CONNECT request")
    return authority[:-4]


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay bounded opaque TLS chunks with downstream backpressure."""
    while chunk := await reader.read(_RELAY_CHUNK_BYTES):
        writer.write(chunk)
        await writer.drain()


async def relay_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    select_host: SelectHost,
    resolve: Resolve,
) -> None:
    """Admit one tunnel, dial the pinned address, and relay until either side closes.

    Bounds the header size, concurrent connections, setup time, tunnel lifetime and
    relay buffers. A refused request gets one fixed ``403`` and no detail.
    """
    global _active
    if _active >= _MAX_CONNECTIONS:
        writer.close()
        return
    _active += 1
    upstream = None
    established = False
    try:
        async with asyncio.timeout(_SETUP_TIMEOUT_SECONDS):
            host = select_host(await reader.readuntil(b"\r\n\r\n"))
            address = await resolve(host)
            remote, upstream = await asyncio.open_connection(address, 443, limit=32768)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            established = True
        tasks = [asyncio.create_task(_relay(reader, upstream)), asyncio.create_task(_relay(remote, writer))]
        try:
            async with asyncio.timeout(_TUNNEL_LIFETIME_SECONDS):
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        if not established:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            with suppress(OSError):
                await writer.drain()
    finally:
        _active -= 1
        writer.close()
        if upstream is not None:
            upstream.close()
        with suppress(OSError):
            await writer.wait_closed()
