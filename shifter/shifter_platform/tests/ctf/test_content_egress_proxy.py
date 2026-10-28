"""The CTF content egress proxy relays HTTPS to public hosts only (#2090).

Organizer image copies and webhooks reach organizer-chosen hosts through this
credential-less CONNECT proxy, the only platform pod with public egress. It
admits DNS names on port 443, refuses IP literals and userinfo, requires every
DNS answer to be a global address, and dials the admitted address.
"""

from __future__ import annotations

import asyncio

import pytest

from ctf import egress_proxy


@pytest.mark.parametrize("host", ["img.example.test", "cdn.images.example.org", "a.b-c.example.com"])
def test_admits_dns_names_on_https(host):
    assert egress_proxy.connect_host(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode()) == host


@pytest.mark.parametrize(
    "authority",
    [
        "127.0.0.1:443",
        "169.254.169.254:443",
        "[::1]:443",
        "img.example.test:80",
        "user@img.example.test:443",
        "img.example.test.:443",
        "localhost:443",
        "metadata.google.internal:443",
        "-bad.example.test:443",
    ],
)
def test_refuses_ip_literals_other_ports_and_special_names(authority):
    with pytest.raises(ValueError):
        egress_proxy.connect_host(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["10.2.0.1", "169.254.169.254", "127.0.0.1", "100.64.0.1"])
async def test_dns_answers_must_all_be_global(monkeypatch, answer):
    loop = asyncio.get_running_loop()

    async def lookup(*args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", (answer, 443))]

    monkeypatch.setattr(loop, "getaddrinfo", lookup)
    with pytest.raises(ValueError, match="address unavailable"):
        await egress_proxy.resolve_public_address("img.example.test")


@pytest.mark.asyncio
async def test_live_listener_refuses_private_destinations():
    server = await asyncio.start_server(egress_proxy.handle_connect, "127.0.0.1", 0, limit=4096)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(b"CONNECT 10.0.0.5:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 403")
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_admitted_tunnel_dials_the_resolved_public_address(monkeypatch):
    real_open = asyncio.open_connection
    dialed = []

    async def echo(reader, writer):
        writer.write(await reader.readexactly(4))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    async with upstream:

        async def resolve(host):
            assert host == "img.example.test"
            return "93.184.216.34"

        async def connect(address, port, **kwargs):
            dialed.append((address, port))
            return await real_open("127.0.0.1", upstream.sockets[0].getsockname()[1], **kwargs)

        monkeypatch.setattr(egress_proxy, "resolve_public_address", resolve)
        monkeypatch.setattr(egress_proxy.asyncio, "open_connection", connect)
        server = await asyncio.start_server(egress_proxy.handle_connect, "127.0.0.1", 0, limit=4096)
        async with server:
            reader, writer = await real_open("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"CONNECT img.example.test:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            assert (
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            )
            writer.write(b"ping")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 2) == b"ping"
            writer.close()
            await writer.wait_closed()
    assert dialed == [("93.184.216.34", 443)]
