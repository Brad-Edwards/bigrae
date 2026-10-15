"""WebSocket consumers for terminal SSH connections and range status updates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User

# RangeStatusConsumer / NGFWStatusConsumer live in status_consumers.py so this
# module stays under Sonar S104's 500-line cap; re-exported here so existing
# imports (mission_control.routing, `from mission_control.consumers import ...`)
# keep resolving.
from mission_control.status_consumers import NGFWStatusConsumer, RangeStatusConsumer
from mission_control.terminal_executor import (
    TerminalExecutorSaturated,
    run_terminal_sync,
)
from mission_control.terminal_sessions import session_registry as _session_registry
from shared.audit import (
    SessionInfo,
    audit_session_event,
    select_trusted_client_ip,
)
from shared.credentials import CredentialContext
from shared.enums import WebSocketCloseCode
from shared.identity_scope import PrincipalRef

if TYPE_CHECKING:
    from shared.remote_access import TerminalConnection, TerminalConnectionFactory

logger = logging.getLogger(__name__)

__all__ = ["NGFWStatusConsumer", "RangeStatusConsumer", "SSHConsumer"]


class SSHConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for SSH terminal connections.

    Bridges browser WebSocket to SSH connection via engine.connect_terminal().

    URL pattern: ws/terminal/<instance_uuid>/
    """

    def __init__(
        self,
        *args: Any,
        connection_factory: TerminalConnectionFactory | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Injected terminal transport factory (issue #993). ``None`` (the
        # production default from ``routing.py``) lets Engine build a real
        # SSHConnection; a test supplies a fake via
        # ``SSHConsumer.as_asgi(connection_factory=...)`` without bypassing the
        # MC -> CMS -> Engine authorization path.
        self._connection_factory = connection_factory
        self.instance_uuid: str | None = None
        self.ssh_conn: TerminalConnection | None = None
        self._read_task: asyncio.Task[None] | None = None
        self.session_id: str = str(uuid.uuid4())[:8]
        self._user_id: int | None = None
        self._principal: PrincipalRef | None = None
        self._capacity_key: int | uuid.UUID | None = None
        self._user_email: str = ""
        self._session_acquired: bool = False
        self._session_start: float = 0.0
        self._last_activity: float = 0.0
        # Bounded reason the session ended; the read loop overwrites it on
        # timeout/EOF/error so the disconnect audit distinguishes a timeout from
        # a plain user-initiated close.
        self._close_reason: str = "user_close"

    async def connect(self) -> None:
        """Handle WebSocket connection request."""
        try:
            await self._do_connect()
        except asyncio.CancelledError:
            await self._disconnect_ssh_best_effort()
            await self._release_session_slot()
            raise
        except Exception:
            logger.error("Terminal connection failed unexpectedly")
            await self._disconnect_ssh_best_effort()
            await self._release_session_slot()
            await self.close(code=WebSocketCloseCode.SERVER_ERROR)

    def _client_ip(self) -> str | None:
        """Best-effort client IP for audit, using the shared trusted-hop policy.

        Mirrors :func:`shared.audit.get_client_ip` on the ASGI scope so
        terminal-session audit rows do not drift from HTTP audit rows: the
        rightmost (proxy-appended) X-Forwarded-For hop is trusted, with the
        direct peer from ``scope["client"]`` as the fallback (SEC-4, issue #937).
        """
        headers = dict(self.scope.get("headers", []))
        xff = headers.get(b"x-forwarded-for", b"").decode()
        client = self.scope.get("client")
        remote_addr = client[0] if client else None
        return select_trusted_client_ip(
            xff,
            remote_addr,
            trusted_hops=getattr(settings, "AUDIT_TRUSTED_PROXY_HOPS", 1),
        )

    async def _resolve_request(self) -> tuple[Any, str] | None:
        """Validate auth and the instance UUID.

        Returns ``(user, instance_uuid)`` on success, or ``None`` after closing
        the socket with the right code when the request is unauthenticated or
        missing an instance UUID, so the caller can bail without branching on
        each failure mode itself.
        """
        user = self.scope.get("credential_context", self.scope.get("user"))
        if not user or isinstance(user, AnonymousUser):
            logger.warning("Unauthenticated terminal connection attempt")
            await self.close(code=WebSocketCloseCode.NOT_AUTHENTICATED)
            return None

        if isinstance(user, CredentialContext):
            self._principal = user.principal
            self._capacity_key = user.principal.uuid
        else:
            self._user_id = user.id
            self._capacity_key = user.id
        self._user_email = getattr(user, "email", "") or ""
        instance_uuid = self.scope["url_route"]["kwargs"].get("instance_uuid")
        if not instance_uuid:
            logger.warning("Terminal connection without instance_uuid")
            await self.close(code=WebSocketCloseCode.INVALID_REQUEST)
            return None
        self.instance_uuid = instance_uuid
        return user, instance_uuid

    async def _audit_best_effort(self, **kwargs: Any) -> None:
        """Write a session-audit event, tolerating terminal-executor saturation.

        Audit writes are best-effort telemetry, not part of the connection
        contract. Under a connect storm the bounded terminal executor may reject
        admission (``TerminalExecutorSaturated``); dropping the audit line is
        preferable to crashing the consumer, so saturation is logged and
        swallowed here while the real connect path surfaces it as a retryable
        close.
        """
        try:
            await run_terminal_sync(audit_session_event, **kwargs)
        except TerminalExecutorSaturated:
            logger.warning(
                "Skipped terminal audit (executor saturated): action=%s uuid=%s",
                kwargs.get("action"),
                self.instance_uuid,
            )

    async def _open_ssh(self, user: Any, instance_uuid: str, client_ip: str | None) -> bool:
        """Open the SSH connection; return True on success.

        On failure, releases the session slot, audits/logs as appropriate, and
        closes the socket with the matching ``WebSocketCloseCode``. Engine owns
        all ownership / range-status / instance validation.
        """
        from cms.services import connect_range_terminal

        try:
            browser = self.scope.get("user")
            browser_proof = isinstance(browser, User) and isinstance(user, CredentialContext)
            # Run blocking connect (DB + Secrets Manager) on the dedicated
            # terminal executor so it cannot block page renders (#929).
            ssh_conn = await run_terminal_sync(
                connect_range_terminal,
                browser if browser_proof else user,
                instance_uuid,
                connection_factory=self._connection_factory,
                **({"credential": user} if browser_proof else {}),
            )
            self.ssh_conn = ssh_conn
            await self._connect_until_revoked(ssh_conn)
            return True
        except TerminalExecutorSaturated:
            await self._release_session_slot()
            logger.warning(
                "Terminal connect rejected - executor saturated: uuid=%s",
                instance_uuid,
            )
            await self.close(code=WebSocketCloseCode.SERVICE_UNAVAILABLE)
        except PermissionError:
            await self._release_session_slot()
            logger.warning("Terminal connection denied - permission error: uuid=%s", instance_uuid)
            await self._audit_best_effort(
                action="access_denied",
                **self._audit_actor(),
                session=SessionInfo(
                    session_id=self.session_id,
                    range_id=None,
                    session_type="terminal",
                    email=getattr(user, "email", "") or "",
                ),
                source_ip=client_ip,
                context=f"Permission denied for instance {instance_uuid}",
            )
            await self.close(code=WebSocketCloseCode.PERMISSION_DENIED)
        except ValueError:
            await self._disconnect_ssh_best_effort()
            await self._release_session_slot()
            logger.warning("Terminal connection unavailable: uuid=%s", instance_uuid)
            await self.close(code=WebSocketCloseCode.NOT_FOUND)
        except Exception:
            await self._disconnect_ssh_best_effort()
            await self._release_session_slot()
            logger.error("SSH connection failed: uuid=%s", instance_uuid)
            await self.close(code=WebSocketCloseCode.SSH_CONNECTION_FAILED)
        return False

    async def _connect_until_revoked(self, connection: TerminalConnection) -> None:
        """Cancel in-progress native connection work when its boundary revokes."""
        revoked = self.scope.get("_authority_revoked")
        if not isinstance(revoked, asyncio.Event):
            await connection.connect()
            return
        connect = asyncio.create_task(connection.connect())
        invalidated = asyncio.create_task(revoked.wait())
        try:
            done, _ = await asyncio.wait((connect, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if invalidated in done:
                connect.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await connect
                await self._disconnect_ssh_best_effort()
                raise PermissionError("Terminal authority revoked")
            await connect
        finally:
            for pending in (connect, invalidated):
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

    async def _do_connect(self) -> None:
        """Authenticate, enforce the session cap, and start the SSH bridge."""
        client_ip = self._client_ip()

        resolved = await self._resolve_request()
        if resolved is None:
            return
        user, instance_uuid = resolved

        logger.debug(
            "Terminal connection requested: user_id=%s instance_uuid=%s",
            self._capacity_key,
            instance_uuid,
        )

        # Enforce the session cap before any expensive SSH work, so a flood of
        # connections (or a reconnect storm) is rejected cheaply instead of
        # exhausting the portal process. SERVICE_UNAVAILABLE (4503) is retryable
        # client-side, so transient pressure self-heals as other sessions end.
        if self._capacity_key is None:
            await self.close(code=WebSocketCloseCode.NOT_AUTHENTICATED)
            return
        self._session_acquired = await _session_registry.try_acquire(
            self._capacity_key,
            settings.TERMINAL_MAX_SESSIONS,
            settings.TERMINAL_MAX_SESSIONS_PER_USER,
        )
        if not self._session_acquired:
            logger.warning(
                "Terminal session cap reached, rejecting: user_id=%s %s",
                self._capacity_key,
                _session_registry.snapshot(),
            )
            await self.close(code=WebSocketCloseCode.SERVICE_UNAVAILABLE)
            return

        if await self._open_ssh(user, instance_uuid, client_ip):
            # Accept WebSocket and start reading SSH output.
            await self.accept()
            loop = asyncio.get_running_loop()
            self._session_start = loop.time()
            self._last_activity = self._session_start

            await self._audit_best_effort(
                action="connect",
                **self._audit_actor(),
                session=SessionInfo(
                    session_id=self.session_id,
                    range_id=None,
                    session_type="terminal",
                    target_ip=instance_uuid,
                    email=self._user_email,
                ),
                source_ip=client_ip,
            )

            logger.info(
                "Terminal connected: user_id=%s uuid=%s %s",
                self._capacity_key,
                instance_uuid,
                _session_registry.snapshot(),
            )

            self._read_task = asyncio.create_task(self._read_ssh_output())

    async def _release_session_slot(self) -> None:
        """Release this consumer's registry slot, exactly once.

        Idempotent so it is safe to call on every failure path and again from
        ``disconnect`` without double-counting.
        """
        key = self._capacity_key if self._capacity_key is not None else self._user_id
        if self._session_acquired and key is not None:
            self._session_acquired = False
            await _session_registry.release(key)

    def _audit_actor(self) -> dict[str, Any]:
        if self._principal is not None:
            return {"user_id": None, "principal": self._principal}
        return {"user_id": self._user_id}

    async def _disconnect_ssh_best_effort(self) -> None:
        """Close the SSH transport once, tolerating transport teardown errors."""
        if self.ssh_conn:
            try:
                await self.ssh_conn.disconnect()
            except Exception:
                logger.error("SSH connection cleanup failed: uuid=%s", self.instance_uuid)
            finally:
                self.ssh_conn = None

    async def _close_websocket_best_effort(self) -> None:
        """Close the WebSocket unless ASGI has already completed the close path."""
        try:
            await self.close()
        except RuntimeError as exc:
            if "websocket.close" not in str(exc):
                raise
            logger.debug(
                "Terminal WebSocket was already closed: uuid=%s",
                self.instance_uuid,
            )

    async def _read_ssh_output(self) -> None:
        """Background task: forward SSH output to the WebSocket.

        Reads with a multi-second poll timeout instead of a tight spin, so an
        idle terminal costs almost no CPU (the previous hard-coded 0.1s poll
        woke every idle session ~10x/second, and spun at full speed once the
        shell hit EOF). Output is still delivered the moment it arrives; the
        timeout only bounds how often the loop wakes to enforce the idle and
        max-duration limits and to notice a closed shell. See issue #847.
        """
        conn = self.ssh_conn
        if conn is None:
            return
        loop = asyncio.get_running_loop()
        if not self._session_start:
            self._session_start = loop.time()
        if not self._last_activity:
            self._last_activity = loop.time()

        poll = settings.TERMINAL_READ_POLL_SECONDS
        idle_timeout = settings.TERMINAL_IDLE_TIMEOUT_SECONDS
        max_duration = settings.TERMINAL_MAX_SESSION_SECONDS
        try:
            while conn.is_connected:
                data = await conn.receive(timeout=poll)
                if data:
                    # receive() returns bytes, decode to string for JSON
                    output = data.decode("utf-8", errors="replace")
                    await self.send(text_data=json.dumps({"type": "output", "data": output}))
                    self._last_activity = loop.time()
                elif conn.at_eof():
                    logger.info("Terminal shell closed (EOF): uuid=%s", self.instance_uuid)
                    self._close_reason = "shell_eof"
                    break

                now = loop.time()
                if idle_timeout > 0 and now - self._last_activity >= idle_timeout:
                    logger.info(
                        "Terminal idle timeout: uuid=%s idle_seconds=%s",
                        self.instance_uuid,
                        idle_timeout,
                    )
                    self._close_reason = "idle_timeout"
                    break
                if max_duration > 0 and now - self._session_start >= max_duration:
                    logger.info(
                        "Terminal max-duration reached: uuid=%s max_seconds=%s",
                        self.instance_uuid,
                        max_duration,
                    )
                    self._close_reason = "max_duration"
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("SSH output failed: uuid=%s", self.instance_uuid)
            self._close_reason = "error"
        finally:
            # The client may already have disconnected by the time the read loop
            # exits. Release scarce terminal resources before asking ASGI to
            # close the socket so a close-race cannot strand a process-local
            # session slot and pin the user at the per-worker cap.
            await self._disconnect_ssh_best_effort()
            await self._release_session_slot()
            await self._close_websocket_best_effort()

    async def disconnect(self, close_code: int) -> None:
        """Handle WebSocket disconnection - cleanup SSH connection."""
        logger.debug(
            "Terminal disconnected: uuid=%s code=%s",
            self.instance_uuid,
            close_code,
        )

        # Cancel read task if running
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task

        # Close SSH connection
        await self._disconnect_ssh_best_effort()

        # Free the session slot now that this session's resources are released.
        await self._release_session_slot()

        # Audit log disconnection if we had a valid session
        if self._user_id is not None or self._principal is not None:
            await self._audit_best_effort(
                action="disconnect",
                **self._audit_actor(),
                session=SessionInfo(
                    session_id=self.session_id,
                    session_type="terminal",
                    email=self._user_email,
                ),
                context=f"close_code={close_code} reason={self._close_reason}",
            )

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        """Handle incoming WebSocket messages - forward to SSH."""
        if not self.ssh_conn or not text_data:
            return

        try:
            message = json.loads(text_data)
            msg_type = message.get("type")

            if msg_type == "input":
                data = message.get("data", "")
                # send() expects bytes
                await self.ssh_conn.send(data.encode("utf-8"))
                self._last_activity = asyncio.get_running_loop().time()

            elif msg_type == "resize":
                cols = message.get("cols", 80)
                rows = message.get("rows", 24)
                await self.ssh_conn.resize(cols, rows)
                self._last_activity = asyncio.get_running_loop().time()

        except json.JSONDecodeError:
            logger.warning("Invalid JSON from terminal: uuid=%s", self.instance_uuid)
        except Exception:
            logger.error("Terminal input failed: uuid=%s", self.instance_uuid)
