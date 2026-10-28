"""CONNECT-only provider egress, with fixed origins and pinned public DNS answers.

TLS terminates at the provider. This process has no cloud identity, credentials,
Django configuration, or access to the control listener.
"""

import asyncio
import ipaddress
import os
import re
import socket

from shared.connect_egress import parse_connect_authority, relay_connect

_ALLOWED_HOSTS = frozenset(
    {"api.openai.com", "api.anthropic.com", "openrouter.ai", "oauth2.googleapis.com", "iamcredentials.googleapis.com"}
)
_CLOUD_HOST = re.compile(
    r"(?:[a-z][a-z0-9-]{1,62}-aiplatform\.googleapis\.com|aiplatform\.(?:us|eu)\.rep\.googleapis\.com|"
    r"(?:sts|bedrock-runtime)\.[a-z]{2}(?:-gov)?-[a-z]+-\d\.amazonaws\.com)"
)


def connect_host(header: bytes) -> str:
    """Accept only an exact approved HTTPS authority, never a URL or userinfo."""
    host = parse_connect_authority(header)
    if host not in _ALLOWED_HOSTS and _CLOUD_HOST.fullmatch(host) is None:
        raise ValueError("provider origin unavailable")
    return host


async def resolve_public_address(host: str) -> str:
    """Resolve once and connect to the admitted IP, preventing DNS rebinding."""
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    private = tuple(
        ipaddress.IPv4Network(value, strict=True)
        for value in os.environ.get("MODEL_EGRESS_PRIVATE_CIDRS", "").split(",")
        if value
    )

    def allowed(value: str) -> bool:
        """Allow public addresses or explicitly admitted AWS private endpoints."""
        address = ipaddress.ip_address(value)
        return address.is_global or (host.endswith(".amazonaws.com") and any(address in network for network in private))

    public = [item[4][0] for item in addresses if allowed(item[4][0])]
    if not public or len(public) != len(addresses):
        raise ValueError("provider address unavailable")
    return public[0]


async def handle_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay one approved provider tunnel through the shared bounded CONNECT core."""
    # Late binding keeps this module's policy functions the single patch point.
    await relay_connect(
        reader,
        writer,
        select_host=lambda header: connect_host(header),
        resolve=lambda host: resolve_public_address(host),
    )


async def main() -> None:
    """Listen on the configured private address with bounded CONNECT headers."""
    from shared.model_access.network import private_listener_address

    address = private_listener_address(os.environ["MODEL_EGRESS_BIND_ADDRESS"])
    server = await asyncio.start_server(handle_connect, address, 3128, limit=4096)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
