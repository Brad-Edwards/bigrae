"""CONNECT-only egress for CTF organizer image copies and webhooks (#2090).

The platform namespace denies public egress. This credential-less process is the
one platform pod allowed to reach public HTTPS hosts, and the CTF scheduler and
portal reach organizer-chosen hosts only through it. It admits a DNS name on port
443, refuses IP literals, userinfo and special names, requires every DNS answer
to be a global address, and dials that pinned address. TLS terminates at the
destination. The process has no cloud identity, credentials or Django settings.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket

from shared.connect_egress import parse_connect_authority, relay_connect

# A lowercase DNS name with at least two labels and an alphabetic top-level label;
# IP literals, trailing dots, and userinfo never match.
_HOSTNAME = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BLOCKED_NAMES = frozenset({"localhost", "metadata.google.internal"})
_LISTEN_PORT = 3128


def connect_host(header: bytes) -> str:
    """Admit only a public DNS name on port 443."""
    host = parse_connect_authority(header)
    if _HOSTNAME.fullmatch(host) is None or host in _BLOCKED_NAMES:
        raise ValueError("destination unavailable")
    return host


async def resolve_public_address(host: str) -> str:
    """Resolve once; every answer must be global; return the address to dial."""
    answers = await asyncio.get_running_loop().getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    addresses = [item[4][0] for item in answers]
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("destination address unavailable")
    return addresses[0]


async def handle_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay one admitted tunnel through the shared bounded CONNECT core."""
    # Late binding keeps this module's policy functions the single patch point.
    await relay_connect(
        reader,
        writer,
        select_host=lambda header: connect_host(header),
        resolve=lambda host: resolve_public_address(host),
    )


async def main() -> None:
    """Listen on the pod's private address."""
    from shared.model_access.network import private_listener_address

    address = private_listener_address(os.environ["CTF_EGRESS_BIND_ADDRESS"])
    server = await asyncio.start_server(handle_connect, address, _LISTEN_PORT, limit=4096)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
