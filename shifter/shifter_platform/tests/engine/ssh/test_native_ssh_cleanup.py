"""Native SSH cancellation closes the data plane, including incomplete shells."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest

from engine.ssh import SSHConnection, SSHConnectionError


@pytest.mark.asyncio
async def test_cancelled_shell_creation_closes_the_native_ssh_transport(monkeypatch):
    lost, started = asyncio.Event(), asyncio.Event()
    key = asyncssh.generate_private_key("ssh-ed25519")

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False

        def connection_lost(self, exc):
            lost.set()

    async def stalled_shell(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncssh.SSHClientConnection, "create_process", stalled_shell)
    async with asyncssh.listen("127.0.0.1", 0, server_factory=Server, server_host_keys=[key]) as listener:
        connection = SSHConnection(
            "127.0.0.1",
            "synthetic",
            key.export_private_key().decode(),
            port=listener.get_port(),
            host_public_key=key.export_public_key().decode(),
        )
        pending = asyncio.create_task(connection.connect())
        try:
            await asyncio.wait_for(started.wait(), 0.5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not connection.is_connected
            await asyncio.wait_for(lost.wait(), 0.5)
        finally:
            if not pending.done():
                pending.cancel()
            await connection.disconnect()


@pytest.mark.asyncio
async def test_unresponsive_close_aborts_and_releases_references(monkeypatch):
    import engine.ssh as ssh

    monkeypatch.setattr(ssh, "_CLOSE_TIMEOUT_SECONDS", 0.02, raising=False)
    transport = MagicMock()
    transport.wait_closed = AsyncMock(side_effect=lambda: asyncio.Event().wait())

    async def stall():
        await asyncio.Event().wait()

    transport.wait_closed = stall
    connection = SSHConnection("192.0.2.10", "synthetic", "synthetic-key")
    connection._conn = transport
    await asyncio.wait_for(connection.disconnect(), 0.2)
    transport.abort.assert_called_once()
    assert connection._conn is None


@pytest.mark.asyncio
async def test_failed_shell_does_not_leave_transport_or_echo_provider_secret(monkeypatch, caplog):
    transport = MagicMock()
    transport.wait_closed = AsyncMock()
    transport.create_process = AsyncMock(side_effect=OSError("SYNTHETIC_SSH_SECRET_MARKER"))
    monkeypatch.setattr(asyncssh, "import_private_key", lambda value: object())
    monkeypatch.setattr(asyncssh, "connect", AsyncMock(return_value=transport))
    connection = SSHConnection("192.0.2.10", "synthetic", "synthetic-key")
    with caplog.at_level(logging.ERROR), pytest.raises(SSHConnectionError) as caught:
        await connection.connect()
    assert "SYNTHETIC_SSH_SECRET_MARKER" not in str(caught.value)
    assert "SYNTHETIC_SSH_SECRET_MARKER" not in caplog.text
    assert connection._conn is None
    transport.close.assert_called_once()
