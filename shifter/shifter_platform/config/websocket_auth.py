"""WebSocket account-origin authorization boundary."""

import asyncio
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User

from shared.enums import WebSocketCloseCode
from shared.remote_access import TERMINAL_TARGET_PATH_RE

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

        async def allowed(*, touch: bool = False) -> bool:
            return await self._revalidate(scope, validation_lock, touch=touch)

        if not await allowed():
            await send({"type": _WEBSOCKET_CLOSE, "code": WebSocketCloseCode.NOT_AUTHENTICATED})
            return

        closed = False
        revoked = asyncio.Event()
        scope["_authority_revoked"] = revoked

        async def close_revoked() -> None:
            """Close the socket once when its credential is no longer valid."""
            nonlocal closed
            if not closed:
                closed = True
                revoked.set()
                await send({"type": _WEBSOCKET_CLOSE, "code": WebSocketCloseCode.NOT_AUTHENTICATED})

        async def guarded_receive() -> ASGIMessage:
            """Reject the next client message after session revocation."""
            if closed:
                return {"type": _WEBSOCKET_DISCONNECT, "code": WebSocketCloseCode.NOT_AUTHENTICATED}
            message = await _receive_or_revoke(receive, revoked)
            if message.get("type") != _WEBSOCKET_DISCONNECT and not await allowed(
                touch=message.get("type") == "websocket.receive"
            ):
                await close_revoked()
                return {"type": _WEBSOCKET_DISCONNECT, "code": WebSocketCloseCode.NOT_AUTHENTICATED}
            return message

        async def guarded_send(message: ASGIMessage) -> None:
            """Suppress application messages after session revocation."""
            nonlocal closed
            if closed:
                return
            if message.get("type") != _WEBSOCKET_CLOSE and not await allowed():
                await close_revoked()
                return
            if message.get("type") == _WEBSOCKET_CLOSE:
                closed = True
            await send(message)

        async def monitor() -> None:
            while not closed:
                await asyncio.sleep(self.recheck_seconds)
                if not closed and not await allowed():
                    await close_revoked()
                    return

        watcher = asyncio.create_task(monitor())
        try:
            await self.application(scope, guarded_receive, guarded_send)
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
                if not await database_sync_to_async(validate_socket_session)(checked_scope, touch=touch):
                    return False
                if self.resource_authorizer is not None:
                    if not await self.resource_authorizer(checked_scope):
                        return False
                else:
                    from config.socket_authority import authorize_socket_resource

                    if not await database_sync_to_async(authorize_socket_resource)(checked_scope):
                        return False
                scope.update(checked_scope)
                return True
        except Exception:
            return False


async def _receive_or_revoke(receive: ASGIReceive, revoked: asyncio.Event) -> ASGIMessage:
    """Wake an idle consumer through its disconnect handler, cancelling the loser."""
    incoming = asyncio.create_task(receive())
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
