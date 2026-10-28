"""Real xrdp transport closure through the supported Guacamole decorator."""

import asyncio
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from mission_control.guacamole import GuacRDPUrlRequest
from tests.integration.guacamole_protocol_fixture import Instructions, encode
from tests.integration.test_guacamole_native_termination import authority_gateway, docker

__all__ = ["authority_gateway"]


async def wait_for_guest(guest):
    deadline = time.monotonic() + 30
    while True:
        try:
            _, writer = await asyncio.open_connection(guest, 3389)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            if time.monotonic() >= deadline:
                pytest.fail("synthetic xrdp did not start")
            await asyncio.sleep(0.2)


@pytest.fixture
def rdp_guest(authority_gateway):
    _, control = authority_gateway
    container = docker("run", "--detach", "--network", control["network"], "shifter-synthetic-rdp-2319:latest")
    try:
        host = docker("inspect", container, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}")
        yield host, container
    finally:
        docker("rm", "--force", container, check=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deny", "outage"])
async def test_gateway_expiry_closes_native_rdp_transport(authority_gateway, rdp_guest, failure):
    (client, endpoint, gateway), control = authority_gateway
    guest, container = rdp_guest
    native_closed = asyncio.Event()
    native_connected = asyncio.Event()
    transports = set()

    async def proxy(reader, writer):
        upstream_reader, upstream_writer = await asyncio.open_connection(guest, 3389)
        transports.add(upstream_writer)
        native_connected.set()

        async def pump(source, target):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()

        tasks = [asyncio.create_task(pump(reader, upstream_writer)), asyncio.create_task(pump(upstream_reader, writer))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()
            writer.close()
            await upstream_writer.wait_closed()
            transports.remove(upstream_writer)
            native_closed.set()

    # Wait for xrdp startup independently of Guacamole's session admission.
    await wait_for_guest(guest)

    binding = str(uuid4())
    control["bindings"][binding] = "allow"
    async with await asyncio.start_server(proxy, gateway, 0) as server:
        port = server.sockets[0].getsockname()[1]
        request = GuacRDPUrlRequest(
            username="access-" + binding,
            connection_name="synthetic-rdp",
            hostname=gateway,
            port=port,
            rdp_username="synthetic",
            rdp_password="synthetic-test-only",
            sftp_enabled=False,
            security="rdp",
        )
        url = await asyncio.to_thread(client.create_rdp_url, request)
        token = parse_qs(urlparse(url).fragment.partition("?")[2])["token"][0]
        query = urlencode(
            {
                "token": token,
                "GUAC_DATA_SOURCE": "json",
                "GUAC_ID": request.connection_name,
                "GUAC_TYPE": "c",
                "GUAC_WIDTH": "800",
                "GUAC_HEIGHT": "600",
                "GUAC_DPI": "96",
            }
        )
        async with connect(
            endpoint.replace("http://", "ws://") + "/websocket-tunnel?" + query, subprotocols=["guacamole"]
        ) as socket:
            desktop = asyncio.Event()

            async def receive():
                instructions = Instructions()
                async for message in socket:
                    for instruction in instructions.feed(message):
                        opcode, *arguments = instruction
                        if opcode in {"png", "copy", "img"}:
                            desktop.set()
                        if opcode == "sync":
                            await socket.send(encode("sync", arguments[0]))
                        if opcode == "blob":
                            await socket.send(encode("ack", arguments[0], "OK", "0"))

            reader = asyncio.create_task(receive())

            async def keepalive():
                # Match both Apache Client's guacd keepalive and the
                # WebSocketTunnel's independent transport ping.
                while True:
                    await socket.send(encode("nop"))
                    await socket.send(encode("", "ping", str(int(time.time() * 1000))))
                    await asyncio.sleep(5)

            pings = asyncio.create_task(keepalive())
            try:
                await asyncio.wait_for(native_connected.wait(), 10)
                await asyncio.wait_for(desktop.wait(), 20)
                # A login/error screen is not a qualified desktop. Keep the
                # transport alive beyond guacd's unresponsive-client timeout and
                # verify a native guest desktop process before withdrawing.
                await asyncio.sleep(35)
                desktop_pid = await asyncio.to_thread(docker, "exec", container, "pgrep", "-u", "synthetic", "xterm")
                assert desktop_pid
                assert transports and not native_closed.is_set()
                started = time.monotonic()
                control["bindings"][binding] = failure
                bound = 85 if failure == "deny" else 165
                await asyncio.wait_for(native_closed.wait(), bound)
                assert not transports
                # Inspect the guest's native socket table as well as proxy EOF.
                connections = await asyncio.to_thread(
                    docker, "exec", container, "ss", "-Htn", "state", "established", "sport", "=", ":3389"
                )
                assert connections == ""
                assert control["calls"].count(binding) >= 2
                print(f"native RDP {failure}: guest transport closed after {time.monotonic() - started:.2f}s")  # noqa: T201
            finally:
                reader.cancel()
                pings.cancel()
                with suppress(asyncio.CancelledError, ConnectionClosed):
                    await reader
                with suppress(asyncio.CancelledError, ConnectionClosed):
                    await pings
