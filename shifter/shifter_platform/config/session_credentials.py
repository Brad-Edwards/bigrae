"""Bounded provider assurance shared by HTTP and Channels session admission."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, cast
from uuid import UUID

from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, get_user, logout
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse

from shared.principal_port import PrincipalResolutionError, principal_for_user

if TYPE_CHECKING:
    from uuid import UUID

    from shared.credentials import CredentialContext


IDENTITY_BACKEND = "config.identity_platform.IdentityPlatformBackend"
ASSURANCE_KEY = "identity_assurance"


class IdentityAssurance(TypedDict):
    """Narrow verified identity and timing evidence retained in a session."""

    issuer: str
    subject: str
    auth_time: float
    created: float
    tenant: str
    seen: float
    checked: float


def establish_identity_session(session: SessionBase, claims: dict[str, object], *, now: float | None = None) -> None:
    """Persist only verified identity and timing evidence, never the ID token."""
    timestamp = time.time() if now is None else now
    session[ASSURANCE_KEY] = {
        "issuer": claims["iss"],
        "subject": claims["sub"],
        "auth_time": claims["auth_time"],
        "created": claims["auth_time"],
        "tenant": claims.get("tenant", ""),
        "seen": timestamp,
        "checked": timestamp,
    }


def _validated_assurance(session: SessionBase, now: float) -> IdentityAssurance:
    """Validate and narrow persisted assurance state without trusting its shape."""
    raw = session.get(ASSURANCE_KEY)
    if not isinstance(raw, dict):
        raise ValueError("Identity assurance is unavailable")
    issuer = raw.get("issuer")
    subject = raw.get("subject")
    tenant = raw.get("tenant")
    expected_issuer = f"https://securetoken.google.com/{settings.IDENTITY_PLATFORM_PROJECT_ID}"
    if issuer != expected_issuer or tenant != settings.IDENTITY_PLATFORM_TENANT_ID or not isinstance(subject, str):
        raise ValueError("Identity assurance does not match provider configuration")
    times = _validated_assurance_times(raw, now)
    return {
        "issuer": issuer,
        "subject": subject,
        "tenant": tenant,
        "created": times["created"],
        "seen": times["seen"],
        "checked": times["checked"],
        "auth_time": times["auth_time"],
    }


def _validated_assurance_times(raw: dict[str, object], now: float) -> dict[str, float]:
    """Reject malformed, future, and non-finite session timestamps."""
    times: dict[str, float] = {}
    for key in ("created", "seen", "checked", "auth_time"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError("Identity assurance timestamp is invalid")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric > now or numeric < 0:
            raise ValueError("Identity assurance timestamp is invalid")
        times[key] = numeric
    return times


def _recheck_provider_state(state: IdentityAssurance) -> None:
    """Require the provider account to remain enabled, verified, and unrevoked."""
    from config.identity_platform import provider_user_state

    record = provider_user_state(state["subject"])
    if record.disabled or record.email_verified is not True:
        raise ValueError("Provider identity is inactive")
    if record.tokens_valid_after_timestamp / 1000 > state["auth_time"]:
        raise ValueError("Provider identity was revoked")


def _identity_session_valid(
    user: User, session: SessionBase, now: float, *, touch: bool = True, provider_check: bool = True
) -> bool:
    """Validate stored assurance, live binding, time limits, and provider state."""
    from management.services import resolve_user_by_provider_identity

    try:
        state = _validated_assurance(session, now)
        if now - state["created"] >= settings.IDENTITY_SESSION_ABSOLUTE_SECONDS:
            raise ValueError("Identity session exceeded its absolute lifetime")
        if now - state["seen"] >= settings.IDENTITY_SESSION_IDLE_SECONDS:
            raise ValueError("Identity session exceeded its idle lifetime")
        if not resolve_user_by_provider_identity(state["issuer"], state["subject"]).filter(pk=user.pk).exists():
            raise ValueError("Provider identity binding is unavailable")
        if provider_check and now - state["checked"] >= settings.IDENTITY_SESSION_RECHECK_SECONDS:
            _recheck_provider_state(state)
            state["checked"] = now
        if touch:
            state["seen"] = now
        session[ASSURANCE_KEY] = state
        return True
    except Exception:
        # Provider/database outages and malformed state never extend a proof.
        return False


def validate_session(
    user: User | AnonymousUser,
    session: SessionBase,
    *,
    now: float | None = None,
    touch: bool = True,
    provider_check: bool = True,
) -> bool:
    """Recheck local lifecycle every use and provider state at a bounded interval."""
    valid = False
    if isinstance(user, User) and bool(getattr(user, "is_authenticated", False)):
        valid = True
        try:
            principal_for_user(user)
        except PrincipalResolutionError:
            valid = False
        if valid and session.get(BACKEND_SESSION_KEY) == IDENTITY_BACKEND:
            valid = _identity_session_valid(
                user, session, time.time() if now is None else now, touch=touch, provider_check=provider_check
            )
    return valid


def session_credential(user: User, session: SessionBase, *, provider_check: bool = True) -> CredentialContext:
    """Resolve one live Django session into a non-secret operation reference."""
    from shared.authorization import ACTION_CATALOG, CredentialCeiling
    from shared.credentials import CredentialContext, temporary_credential

    try:
        request = cast(HttpRequest, SimpleNamespace(session=session))
        live_user = get_user(request)
        if (
            not isinstance(live_user, User)
            or live_user.pk != user.pk
            or not validate_session(live_user, session, touch=False, provider_check=provider_check)
        ):
            raise ValueError("Session unavailable")
        principal = principal_for_user(live_user)
        if getattr(session, "principal_uuid", None) != principal.uuid:
            raise ValueError("Session reference unavailable")
        reference = getattr(session, "credential_uuid", None)
        if not isinstance(reference, UUID):
            raise ValueError("Session reference unavailable")
        if getattr(getattr(live_user, "profile", None), "is_ctf_account", False):
            temporary = temporary_credential(live_user)
            return CredentialContext(
                principal, "temporary", reference, temporary.ceiling, temporary.scopes, temporary.event_uuid
            )
        return CredentialContext(
            principal,
            "session",
            reference,
            CredentialCeiling(frozenset(action.code for action in ACTION_CATALOG)),
            frozenset(),
        )
    except Exception:
        raise ValueError("Credential unavailable") from None


def resolve_session_credential(credential_uuid: UUID, *, provider_check: bool = True) -> CredentialContext:
    """Recheck logout, expiry, password, provider and principal without renewal."""
    from management.services import session_for_credential

    session = session_for_credential(credential_uuid)
    request = cast(HttpRequest, SimpleNamespace(session=session))
    user = get_user(request)
    if not isinstance(user, User):
        raise ValueError("Credential unavailable")
    # Delayed work must not renew a browser session's idle or framework expiry.
    return session_credential(user, session, provider_check=provider_check)


def validate_socket_session(scope: dict[str, object], *, touch: bool = False) -> bool:
    """Reload Django state so logout/password invalidation also closes sockets."""

    from shared.credentials import CredentialContext, resolve_current_credential

    try:
        current = None
        original = scope.get("credential_context")
        if original is not None:
            if not isinstance(original, CredentialContext):
                raise ValueError("Socket credential is unavailable")
            current = resolve_current_credential(original)
            if original.kind in {"personal", "service"}:
                scope["credential_context"] = current
                return True
        _validate_browser_socket(scope, original, current, touch=touch)
        return True
    except Exception:
        return False


class CredentialSessionMiddleware:
    """Discard invalid sessions before any HTTP authorization boundary runs."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated and not validate_session(request.user, request.session):
            logout(request)
        return self.get_response(request)


def _validate_browser_socket(
    scope: dict[str, object], original: CredentialContext | None, current: CredentialContext | None, *, touch: bool
) -> None:
    """Reload the framework browser session without extending proof lifetime during idle checks."""
    from importlib import import_module

    cached = scope.get("session")
    if not isinstance(cached, SessionBase) or not cached.session_key:
        raise ValueError("Socket session is unavailable")
    session = import_module(settings.SESSION_ENGINE).SessionStore(session_key=cached.session_key)
    request = cast(HttpRequest, SimpleNamespace(session=session))
    user = get_user(request)
    if not isinstance(user, User) or not validate_session(user, session, touch=touch):
        raise ValueError("Socket session is invalid")
    _bind_socket_credential(scope, session, user, original, current)
    if session.modified:
        if touch:
            session.save(must_create=False)
        else:
            from management.services import save_session_assurance

            save_session_assurance(session)
    scope["user"] = user
    scope["session"] = session


def _bind_socket_credential(
    scope: dict[str, object],
    session: SessionBase,
    user: User,
    original: CredentialContext | None,
    current: CredentialContext | None,
) -> None:
    """Keep browser transport bound to the same original principal and framework credential reference."""
    if original is not None:
        if current is None:
            raise ValueError("Socket credential is unavailable")
        admitted = session_credential(user, session)
        if admitted.credential_uuid != original.credential_uuid or admitted.principal != original.principal:
            raise ValueError("Socket session identity changed")
        scope["credential_context"] = current
    else:
        from shared.remote_access import TERMINAL_TARGET_PATH_RE

        if (
            TERMINAL_TARGET_PATH_RE.fullmatch(str(scope.get("path", "")))
            and getattr(session, "credential_uuid", None) is not None
        ):
            scope["credential_context"] = session_credential(user, session)
