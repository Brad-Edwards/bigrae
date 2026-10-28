"""Qualify native Guacamole/guacd tunnel teardown with synthetic SSH and SFTP.

Opt in with GUACAMOLE_NATIVE_INTEGRATION=1. Only the repository's pinned
upstream images run, on an isolated local Docker network and localhost HTTP.
The SSH/SFTP server exposes a disposable synthetic directory, never host files.
"""

import asyncio
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from shutil import which
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import asyncssh
import pytest
from websockets.asyncio.client import connect

from mission_control.guacamole import GuacamoleClientConfig, GuacSSHUrlRequest, JsonAuthGuacamoleClient

_CLIENT_IMAGE = "guacamole/guacamole@sha256:1880e60e0b768294537a7071bc023fffbc8f4917b4f7caf0b72d8938ee4f238f"
_DAEMON_IMAGE = "guacamole/guacd@sha256:8974eaa9ba32f713daf311e7cc8cd7e4cdfba1edea39eed75524e78ef4b08f4f"
_DATABASE_IMAGE = "postgres@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d"
_SIGNING_KEY = "0123456789abcdef0123456789abcdef"  # nosec B105  # NOSONAR


def docker(*args, check=True):
    executable = which("docker")
    if executable is None:
        pytest.fail("local Docker is required for native Guacamole qualification")
    # Every argument is a pinned fixture value or an ID this fixture created.
    result = subprocess.run([executable, *args], check=check, capture_output=True, text=True, timeout=60)  # noqa: S603 # nosec B603
    return (result.stdout + result.stderr if args[0] == "logs" else result.stdout).strip()


@pytest.fixture(scope="module")
def native_gateway():
    if os.environ.get("GUACAMOLE_NATIVE_INTEGRATION") != "1":
        pytest.skip("set GUACAMOLE_NATIVE_INTEGRATION=1 for this local provider qualification")
    prefix = "shifter-2319-guac-" + uuid4().hex[:8]
    containers = []
    docker("network", "create", prefix)
    try:
        gateway = docker("network", "inspect", prefix, "--format", "{{(index .IPAM.Config 0).Gateway}}")
        containers.append(docker("run", "--detach", "--network", prefix, "--network-alias", "guacd", _DAEMON_IMAGE))
        containers.append(
            docker(
                "run",
                "--detach",
                "--network",
                prefix,
                "--publish",
                "127.0.0.1::8080",
                "--env",
                "JSON_ENABLED=true",
                "--env",
                "JSON_SECRET_KEY=" + _SIGNING_KEY,
                "--env",
                "GUACD_HOSTNAME=guacd",
                _CLIENT_IMAGE,
            )
        )
        address = docker("port", containers[-1], "8080/tcp")
        endpoint = f"http://{address}/guacamole"
        # A readiness exchange uses only an empty synthetic connection set.
        client = JsonAuthGuacamoleClient(
            GuacamoleClientConfig(base_url=endpoint, secret_key=_SIGNING_KEY, retry_attempts=1, timeout_seconds=2)
        )
        deadline = time.monotonic() + 45
        from mission_control.guacamole import create_guacamole_auth_payload, sign_and_encrypt_payload

        encrypted = sign_and_encrypt_payload(create_guacamole_auth_payload("synthetic-readiness", {}), _SIGNING_KEY)
        while True:
            try:
                token = client._get_auth_token(encrypted)
                assert client.terminate_session(token)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    pytest.fail("pinned local Guacamole did not become ready")
                time.sleep(0.2)
        yield client, endpoint, gateway
    finally:
        for container in reversed(containers):
            docker("rm", "--force", container, check=False)
        docker("network", "rm", prefix, check=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("sftp", [False, True], ids=["ssh", "ssh-and-sftp"])
@pytest.mark.parametrize("failure", ["delete", "lost-response"])
async def test_native_session_delete_closes_guacd_and_synthetic_guest(native_gateway, tmp_path, sftp, failure):
    await qualify_guest_closure(native_gateway, tmp_path, sftp, failure=failure)


@contextmanager
def discard_native_delete_response(endpoint):
    """Forward a real native DELETE, then lose its acknowledgement on the wire."""

    class Proxy(BaseHTTPRequestHandler):
        def do_DELETE(self):
            request = urllib.request.Request(endpoint + "/api/session", method="DELETE")  # noqa: S310
            request.add_header("Guacamole-Token", self.headers["Guacamole-Token"])
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 # nosec B310
                assert response.status == 204
            self.close_connection = True

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield JsonAuthGuacamoleClient(
            GuacamoleClientConfig(
                base_url=f"http://127.0.0.1:{httpd.server_port}",
                secret_key=_SIGNING_KEY,
                timeout_seconds=5,
            )
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


async def database_identity(control, identity):
    """Confirm the supplementary native database context was actually created."""
    return await asyncio.to_thread(
        docker,
        "exec",
        control["database"],
        "psql",
        "--username",
        "synthetic",
        "--dbname",
        "guacamole",
        "-Atc",
        f"SELECT name FROM guacamole_entity WHERE name = '{identity}'",
    )


async def keep_tunnel_alive(websocket):
    while True:
        await websocket.send("3.nop;")
        timestamp = str(int(time.time() * 1000))
        await websocket.send(f"0.,4.ping,{len(timestamp)}.{timestamp};")
        await asyncio.sleep(5)


async def withdraw_native_authority(client, endpoint, token, *, control, identity, failure):
    if control:
        control["bindings"][identity] = failure
    else:
        if failure == "lost-response":
            with discard_native_delete_response(endpoint) as unreliable:
                assert not await asyncio.to_thread(unreliable.terminate_session, token)
        assert await asyncio.to_thread(client.terminate_session, token)
        assert await asyncio.to_thread(client.terminate_session, token)


async def qualify_guest_closure(native_gateway, tmp_path, sftp, *, control=None, failure="delete"):
    client, endpoint, gateway = native_gateway
    key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
    connected = asyncio.Event()
    sftp_connected = asyncio.Event()
    transports = set()
    closed = asyncio.Event()

    class Server(asyncssh.SSHServer):
        def connection_made(self, connection):
            self.connection = connection
            transports.add(connection)

        def begin_auth(self, username):
            return False

        def connection_lost(self, exc):
            transports.discard(self.connection)
            if not transports:
                closed.set()

    async def process(shell):
        connected.set()
        shell.stdout.write("Synthetic terminal ready\n")
        try:
            await shell.stdin.read()
        finally:
            shell.exit(0)

    def sftp_server(channel):
        sftp_connected.set()
        return asyncssh.SFTPServer(channel, chroot=os.fsencode(tmp_path))

    (tmp_path / "synthetic.txt").write_text("synthetic fixture only")
    async with asyncssh.listen(
        gateway, 0, server_factory=Server, server_host_keys=[key], process_factory=process, sftp_factory=sftp_server
    ) as listener:
        request = GuacSSHUrlRequest(
            username="access-" + str(uuid4()) if control else "synthetic-remote",
            connection_name="synthetic-guest",
            hostname=gateway,
            port=listener.get_port(),
            ssh_username="synthetic",
            ssh_private_key=key.export_private_key().decode(),
        )
        if control:
            control["bindings"][request.username[7:]] = "allow"
        if sftp:
            from mission_control.guacamole import create_ssh_connection_params

            parameters = create_ssh_connection_params(
                username=request.ssh_username,
                hostname=gateway,
                port=request.port,
                ssh_private_key=request.ssh_private_key,
            )
            parameters.update({"enable-sftp": "true", "sftp-root-directory": "/"})
            url = await asyncio.to_thread(
                client._mint_url,
                username=request.username,
                connection_name=request.connection_name,
                connections={request.connection_name: {"protocol": "ssh", "parameters": parameters}},
                expires_minutes=5,
            )
        else:
            url = await asyncio.to_thread(client.create_ssh_url, request)
        assert control is None or await database_identity(control, request.username) == request.username
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
        tunnel_url = endpoint.replace("http://", "ws://") + "/websocket-tunnel?" + query
        async with connect(tunnel_url, subprotocols=["guacamole"], open_timeout=5) as websocket:
            # Receiving native instructions proves this is a real Guacamole
            # tunnel; guest shell admission proves guacd opened the data plane.
            assert await asyncio.wait_for(websocket.recv(), 5)

            async def acknowledge_sync():
                async for message in websocket:
                    for instruction in re.findall(r"4\.sync,[^;]+;", message):
                        await websocket.send(instruction)

            reader = asyncio.create_task(acknowledge_sync())

            pings = asyncio.create_task(keep_tunnel_alive(websocket))
            await asyncio.wait_for(connected.wait(), 5)
            if sftp:
                await asyncio.wait_for(sftp_connected.wait(), 5)
            await asyncio.sleep(35)
            assert transports and not closed.is_set() and not reader.done() and not pings.done()
            started = time.monotonic()
            try:
                await withdraw_native_authority(
                    client, endpoint, token, control=control, identity=request.username[7:], failure=failure
                )
                bound = 85 if failure == "deny" else 165 if failure == "outage" else 20
                await asyncio.wait_for(closed.wait(), bound)
                assert not transports
                elapsed = time.monotonic() - started
                assert elapsed < bound
                if control:
                    assert control["calls"].count(request.username[7:]) >= 2
                    print(f"native SSH/SFTP {failure}: guest closed after {elapsed:.2f}s")  # noqa: T201
            finally:
                reader.cancel()
                pings.cancel()
                await asyncio.gather(reader, pings, return_exceptions=True)


@pytest.fixture(scope="module")
def authority_gateway(tmp_path_factory):
    if os.environ.get("GUACAMOLE_NATIVE_INTEGRATION") != "1":
        pytest.skip("native local integration opt-in required")
    from tests.integration.guacamole_authority_fixture import authority_control

    prefix = "shifter-2319-authority-" + uuid4().hex[:8]
    containers = []
    docker("network", "create", prefix)
    try:
        gateway = docker("network", "inspect", prefix, "--format", "{{(index .IPAM.Config 0).Gateway}}")
        with authority_control(gateway, tmp_path_factory.mktemp("authority-control")) as control:
            control["network"] = prefix
            containers.append(docker("run", "--detach", "--network", prefix, "--network-alias", "guacd", _DAEMON_IMAGE))
            control["daemon"] = containers[-1]
            containers.append(
                docker(
                    "run",
                    "--detach",
                    "--network",
                    prefix,
                    "--network-alias",
                    "guac-db",
                    "--env",
                    "POSTGRES_USER=synthetic",
                    "--env",
                    "POSTGRES_PASSWORD=synthetic-test-only",
                    "--env",
                    "POSTGRES_DB=guacamole",
                    _DATABASE_IMAGE,
                )
            )
            control["database"] = containers[-1]
            deadline = time.monotonic() + 30
            while "accepting connections" not in docker(
                "exec",
                containers[-1],
                "pg_isready",
                "--host",
                "127.0.0.1",
                "--username",
                "synthetic",
                "--dbname",
                "guacamole",
                check=False,
            ):
                if time.monotonic() >= deadline:
                    pytest.fail("synthetic gateway database did not become ready")
                time.sleep(0.2)
            containers.append(
                docker(
                    "run",
                    "--detach",
                    "--network",
                    prefix,
                    "--publish",
                    "127.0.0.1::8080",
                    "--env",
                    "JSON_ENABLED=true",
                    "--env",
                    "JSON_SECRET_KEY=" + _SIGNING_KEY,
                    "--env",
                    "GUACD_HOSTNAME=guacd",
                    "--env",
                    "POSTGRESQL_HOSTNAME=guac-db",
                    "--env",
                    "POSTGRESQL_DATABASE=guacamole",
                    "--env",
                    "POSTGRESQL_USER=synthetic",
                    "--env",
                    "POSTGRESQL_PASSWORD=synthetic-test-only",
                    "--env",
                    "POSTGRESQL_AUTO_CREATE_ACCOUNTS=true",
                    "--env",
                    "OPENFGA_ENABLED=true",
                    "--env",
                    "GUACAMOLE_CONTROL_URL=" + control["url"],
                    "--env",
                    "GUACAMOLE_CONTROL_AUDIENCE=" + control["audience"],
                    "--env",
                    "GCE_METADATA_HOST=" + control["metadata"],
                    "--env",
                    "JAVA_TOOL_OPTIONS=-Djavax.net.ssl.trustStore=/tmp/control.jks "
                    "-Djavax.net.ssl.trustStorePassword=synthetic-test-only",
                    "--volume",
                    str(control["truststore"]) + ":/tmp/control.jks:ro",
                    os.environ.get("GUACAMOLE_EXTENSION_IMAGE", _CLIENT_IMAGE),
                )
            )
            endpoint = "http://" + docker("port", containers[-1], "8080/tcp") + "/guacamole"
            client = JsonAuthGuacamoleClient(
                GuacamoleClientConfig(base_url=endpoint, secret_key=_SIGNING_KEY, retry_attempts=1, timeout_seconds=10)
            )
            from mission_control.guacamole import create_guacamole_auth_payload, sign_and_encrypt_payload

            readiness = str(uuid4())
            control["bindings"][readiness] = "allow"
            encrypted = sign_and_encrypt_payload(create_guacamole_auth_payload("access-" + readiness, {}), _SIGNING_KEY)
            deadline = time.monotonic() + 60
            while True:
                try:
                    token = client._get_auth_token(encrypted)
                    assert client.terminate_session(token)
                    break
                except ValueError:
                    if time.monotonic() >= deadline:
                        pytest.fail("native gateway extension admission did not become ready")
                    time.sleep(0.2)
            yield (client, endpoint, gateway), control
    finally:
        for container in reversed(containers):
            docker("rm", "--force", container, check=False)
        docker("network", "rm", prefix, check=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deny", "outage"])
async def test_gateway_local_expiry_closes_native_ssh_and_sftp(authority_gateway, tmp_path, failure):
    gateway, control = authority_gateway
    await qualify_guest_closure(gateway, tmp_path, True, control=control, failure=failure)
