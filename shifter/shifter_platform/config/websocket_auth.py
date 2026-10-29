"""WebSocket account-origin authorization boundary."""

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User

from shared.enums import WebSocketCloseCode
from shared.exceptions import CMSError
from shared.log_sanitize import safe_log_value
from shared.remote_access import TERMINAL_TARGET_PATH_RE

logger = logging.getLogger(__name__)

type ASGIMessage = dict[str, object]
type ASGIScope = dict[str, object]
type ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
type ASGISend = Callable[[ASGIMessage], Awaitable[None]]
type ASGIApplication = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]

_WEBSOCKET_CLOSE = "websocket.close"
_WEBSOCKET_DISCONNECT = "websocket.disconnect"


class CredentialSessionWebSocketBoundary:
    """Recheck idle, input and output access; deliver revocation to cleanup.

    Source resource authorization uses the owning service composition. Periodic
    validation never counts as user activity. A provider timeout
    denies access even when its synchronous worker cannot yet finish.
    """

    def __init__(
        self,
        application: ASGIApplication,
        *,
        resource_authorizer: Callable[[ASGIScope], Awaitable[bool]] | None = None,
        recheck_seconds: float | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.application = application
        self.resource_authorizer = resource_authorizer
        self.recheck_seconds = (
            min(15.0, settings.IDENTITY_SESSION_RECHECK_SECONDS) if recheck_seconds is None else recheck_seconds
        )
        self.timeout_seconds = timeout_seconds
        if any(not math.isfinite(value) or value <= 0 for value in (self.recheck_seconds, timeout_seconds)):
            raise ValueError("Socket validation requires finite positive bounds")

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        validation_lock = asyncio.Lock()

        async def allowed(touch: bool = False) -> bool:
            """Serialize credential and resource revalidation for this socket."""
            return await self._revalidate(scope, validation_lock, touch=touch)

        if not await allowed():
            await send({"type": _WEBSOCKET_CLOSE, "code": WebSocketCloseCode.NOT_AUTHENTICATED})
            return

        guard = _SocketAuthorityGuard(scope, receive, send, allowed, self.recheck_seconds)
        watcher = asyncio.create_task(guard.monitor())
        try:
            await self.application(scope, guard.receive, guard.send)
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

    async def _revalidate(self, scope: ASGIScope, lock: asyncio.Lock, *, touch: bool) -> bool:
        """Bound provider checks and discard late mutations from timed-out workers."""
        from config.session_credentials import validate_socket_session

        try:
            async with asyncio.timeout(self.timeout_seconds), lock:
                checked_scope = dict(scope)
                if not await database_sync_to_async(validate_socket_session)(
                    checked_scope, touch=touch
                ) or not await self._resource_allowed(checked_scope):
                    return False
                scope.update(checked_scope)
                return True
        except (TimeoutError, ValueError, PermissionError, CMSError) as exc:
            # Expected denial: a provider timeout or a resource the socket may no longer use.
            logger.warning(
                "WebSocket revalidation denied: path=%s exc_type=%s",
                safe_log_value(scope.get("path")),
                type(exc).__name__,
            )
        except Exception:
            logger.exception("WebSocket revalidation failed closed: path=%s", safe_log_value(scope.get("path")))
        return False

    async def _resource_allowed(self, scope: ASGIScope) -> bool:
        """Use the configured resource check or the owning service composition."""
        from config.socket_authority import authorize_socket_resource

        if self.resource_authorizer is not None:
            return await self.resource_authorizer(scope)
        return await database_sync_to_async(authorize_socket_resource)(scope)


class _SocketAuthorityGuard:
    """One connection's revocation signal, guarded transport and periodic recheck."""

    def __init__(
        self,
        scope: ASGIScope,
        receive: ASGIReceive,
        send: ASGISend,
        allowed: Callable[[bool], Awaitable[bool]],
        recheck_seconds: float,
    ) -> None:
        self._receive, self._send, self._allowed = receive, send, allowed
        self._recheck_seconds = recheck_seconds
        self._closed = False
        self._revoked = asyncio.Event()
        scope["_authority_revoked"] = self._revoked

    async def close_revoked(self) -> None:
        """Close once and wake an idle application through its disconnect handler."""
        if not self._closed:
            self._closed = True
            self._revoked.set()
            await self._send({"type": _WEBSOCKET_CLOSE, "code": WebSocketCloseCode.NOT_AUTHENTICATED})

    async def receive(self) -> ASGIMessage:
        """Reject client input after revocation and count only live input as activity."""
        denied = {"type": _WEBSOCKET_DISCONNECT, "code": WebSocketCloseCode.NOT_AUTHENTICATED}
        if self._closed:
            return denied
        message = await _receive_or_revoke(self._receive, self._revoked)
        if message.get("type") != _WEBSOCKET_DISCONNECT and not await self._allowed(
            message.get("type") == "websocket.receive"
        ):
            await self.close_revoked()
            return denied
        return message

    async def send(self, message: ASGIMessage) -> None:
        """Suppress application output after credential or resource access expires."""
        if not self._closed:
            if message.get("type") != _WEBSOCKET_CLOSE and not await self._allowed(False):
                await self.close_revoked()
            else:
                if message.get("type") == _WEBSOCKET_CLOSE:
                    self._closed = True
                    self._revoked.set()
                await self._send(message)

    async def monitor(self) -> None:
        """Wait on revocation and recheck idle authority at a bounded interval."""
        while not self._closed:
            try:
                async with asyncio.timeout(self._recheck_seconds):
                    await self._revoked.wait()
                return
            except TimeoutError:
                pass
            if not self._closed and not await self._allowed(False):
                await self.close_revoked()
                return


async def _receive_or_revoke(receive: ASGIReceive, revoked: asyncio.Event) -> ASGIMessage:
    """Wake an idle consumer through its disconnect handler, cancelling the loser."""
    incoming = asyncio.ensure_future(receive())
    interrupted = asyncio.create_task(revoked.wait())
    try:
        done, _ = await asyncio.wait((incoming, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if interrupted in done:
            return {"type": _WEBSOCKET_DISCONNECT, "code": WebSocketCloseCode.NOT_AUTHENTICATED}
        return incoming.result()
    finally:
        for task in (incoming, interrupted):
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class CTFAccountWebSocketBoundary:
    """Restrict temporary CTF accounts to their participant terminal socket."""

    def __init__(self, application: ASGIApplication) -> None:
        self.application = application

    async def __call__(self, scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        user = scope.get("user")
        if isinstance(user, (User, AnonymousUser)) and await self._is_ctf_account(user):
            path = str(scope.get("path", ""))
            allowed_path = bool(TERMINAL_TARGET_PATH_RE.fullmatch(path)) or path == "/ws/notifications/"
            if not allowed_path or not await self._may_access_terminal(user):
                await send({"type": _WEBSOCKET_CLOSE, "code": 4403})
                return
        await self.application(scope, receive, send)

    @database_sync_to_async
    def _is_ctf_account(self, user: User | AnonymousUser) -> bool:
        from management.services import is_temporary_ctf_account

        return is_temporary_ctf_account(user)

    @database_sync_to_async
    def _may_access_terminal(self, user: User | AnonymousUser) -> bool:
        """Mirror the HTTP participant boundary for the terminal WebSocket."""
        from ctf.services.participant.accounts import live_participant_for_user
        from management.services import is_ctf_password_change_required

        return (
            isinstance(user, User)
            and live_participant_for_user(user) is not None
            and not is_ctf_password_change_required(user)
        )
