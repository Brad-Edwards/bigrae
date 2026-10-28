"""Mission Control Guacamole remote-access session application service.

One transport-neutral use-case entry point, :func:`launch_guacamole_session`,
owns the Guacamole browser-session orchestration that the DRF views used to
perform inline (issue #991). Given the authenticated actor, a closed access
kind (:class:`~mission_control.models.GuacamoleBootstrapRequest.Protocol`), and
an opaque target identifier, it:

1. binds the Guacamole runtime configuration (signing secret + base/API URLs);
2. enqueues the existing bounded asynchronous bootstrap (#929) so credential
   resolution and the Guacamole token exchange run off the request thread; and
3. inside that worker, per the access kind, resolves the sanctioned public
   ``engine.services`` connection projection, adapts it into the existing
   ``mission_control.guacamole`` request dataclass, and calls the existing
   Guacamole broker. The existing bootstrap lifecycle persists and delivers the
   resulting URL.

The per-protocol resolve-and-mint building blocks live in
:mod:`mission_control._guacamole_session_builders` (split for Sonar S104's
500-line cap); this module keeps the HTTP-neutral entry point, the closed
access-kind dispatch, and the enqueue glue.

The service is HTTP-neutral: it never builds a ``JsonResponse``, reverses a
URL, or re-parses a response body. Synchronous configuration/readiness failures
and worker-side orchestration failures both surface as
:class:`~mission_control.guacamole_bootstrap.BootstrapFailure` (a safe message
plus an HTTP status code) -- the DRF adapter renders the former and the
bootstrap worker persists the latter, so there is no separate exception
hierarchy. This keeps each view a thin HTTP adapter (authenticate, validate,
one service call, render) while consuming only the ADR-001-R4 allowlisted
``engine.services`` symbols.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from mission_control._guacamole_session_builders import (
    _build_ngfw_ssh_url,
    _build_range_ssh_url,
    _build_rdp_url,
)
from mission_control.guacamole_bootstrap import BootstrapFailure, BootstrapQueueFull, enqueue_guacamole_bootstrap
from mission_control.models import GuacamoleBootstrapRequest
from shared.credentials import CredentialContext
from shared.exceptions import CMSError
from shared.log_sanitize import safe_log_fingerprint, safe_log_value

if TYPE_CHECKING:
    from mission_control.guacamole import GuacamoleClient

logger = logging.getLogger(__name__)

_GUAC_AUTH_NOT_CONFIGURED = "Guacamole JSON auth is not configured"
_GUACAMOLE_BASE_PATH = "/guacamole"


def _bind_guacamole_client(service_name: str, *, require_signing_key: bool = True) -> GuacamoleClient:
    """Bind Guacamole runtime configuration into a client, or raise a 503.

    Runs synchronously on the request thread so a missing signing secret fails
    closed before any bootstrap is enqueued (matching the prior view behaviour).
    Reads the existing ``GUACAMOLE_*`` settings once at this application-service
    edge; the client and its retry loop never read Django settings (issue #993).
    """
    from django.conf import settings

    from mission_control.guacamole import GuacamoleClientConfig, get_guacamole_client

    signing_secret = getattr(settings, "GUACAMOLE_JSON_AUTH_SECRET", "")
    if require_signing_key and not signing_secret:
        logger.error(_GUAC_AUTH_NOT_CONFIGURED)
        raise BootstrapFailure(f"{service_name} service not configured", status_code=503)
    config = GuacamoleClientConfig(
        base_url=getattr(settings, "GUACAMOLE_BASE_URL", _GUACAMOLE_BASE_PATH),
        secret_key=signing_secret,
        api_base_url=getattr(settings, "GUACAMOLE_API_BASE_URL", None),
        retry_attempts=getattr(settings, "GUACAMOLE_TOKEN_RETRY_ATTEMPTS", 3),
        retry_base_delay_ms=getattr(settings, "GUACAMOLE_TOKEN_RETRY_BASE_DELAY_MS", 200),
    )
    return get_guacamole_client(config)


@dataclass(frozen=True)
class GuacamoleSessionLaunch:
    """HTTP-neutral result of enqueuing a Guacamole bootstrap.

    Carries only the enqueued bootstrap facts the launch adapter needs to render
    the 202 response. The final bearer URL is never returned here; it remains
    available only from the owner-scoped, single-consume status endpoint.
    """

    bootstrap_id: UUID
    status: str


def _service_name(protocol: str) -> str:
    """Return the user-facing service label for the not-configured message."""
    return "RDP" if protocol == GuacamoleBootstrapRequest.Protocol.RDP else "SSH"


def _worker_build_callable(
    *,
    protocol: str,
    user: CredentialContext,
    target_id: str,
    guac_client: GuacamoleClient,
    session_identity: str | None = None,
    range_uuid: UUID | None = None,
) -> Callable[[], str]:
    """Return the closed-access-kind worker callable that builds the signed URL.

    The dispatch keeps range SSH and NGFW SSH distinct (they share the Guacamole
    SSH transport but not their ownership/connection-name policy). Adding a new
    browser-session kind adds one branch here plus its resolver/adapter, reusing
    the same bootstrap, delivery, client, error, and logging envelopes.
    """
    protocols = GuacamoleBootstrapRequest.Protocol
    options: dict[str, Any] = {}
    if session_identity is not None:
        options["session_identity"] = session_identity
    range_options = {**options, "range_uuid": range_uuid} if session_identity is not None else options
    if protocol == protocols.RDP:
        return lambda: _build_rdp_url(user=user, instance_uuid=target_id, guac_client=guac_client, **range_options)
    if protocol == protocols.RANGE_SSH:
        return lambda: _build_range_ssh_url(
            user=user, instance_uuid=target_id, guac_client=guac_client, **range_options
        )
    if protocol == protocols.NGFW_SSH:
        return lambda: _build_ngfw_ssh_url(user=user, app_id=target_id, guac_client=guac_client, **options)
    raise ValueError(f"Unsupported Guacamole access kind: {protocol!r}")


def _bootstrap_authority(
    user: CredentialContext, protocol: str, target_id: str, range_uuid: UUID | None
) -> tuple[dict[str, object], UUID]:
    """Authorize the exact session target before allocating its queue identity."""
    from cms.services import admit_device_remote_authority, admit_range_remote_authority

    try:
        if protocol == GuacamoleBootstrapRequest.Protocol.NGFW_SSH:
            authority = admit_device_remote_authority(user, target_id)
        else:
            authority = admit_range_remote_authority(user, target_id, range_uuid=range_uuid)
    except (CMSError, PermissionError, ValueError):
        raise BootstrapFailure("Permission denied", status_code=403) from None
    return authority, uuid4()


def launch_guacamole_session(
    *,
    user: CredentialContext,
    protocol: str,
    target_id: str,
    guacamole_client: GuacamoleClient | None = None,
    range_uuid: UUID | None = None,
) -> GuacamoleSessionLaunch:
    """Launch a Guacamole browser session for ``user`` against ``target_id``.

    Binds Guacamole configuration into a client (or uses the injected
    ``guacamole_client`` — the explicit, keyword-only test seam, issue #993),
    enqueues the bounded async bootstrap (#929), and returns the HTTP-neutral
    :class:`GuacamoleSessionLaunch`. Raises
    :class:`~mission_control.guacamole_bootstrap.BootstrapFailure` for a
    synchronous readiness/config failure (e.g. missing signing secret -> 503)
    and :class:`~mission_control.guacamole_bootstrap.BootstrapQueueFull` when
    the worker pool is saturated; the launch adapter renders both. Worker-side
    resolution/generation failures are persisted on the bootstrap row and
    surfaced by the status endpoint, not raised here.
    """
    if not isinstance(user, CredentialContext):
        raise BootstrapFailure("Permission denied", status_code=403)
    if protocol not in GuacamoleBootstrapRequest.Protocol.values:
        raise ValueError(f"Unsupported Guacamole access kind: {protocol!r}")
    guac_client = guacamole_client or _bind_guacamole_client(_service_name(protocol))
    authority, bootstrap_id = _bootstrap_authority(user, protocol, target_id, range_uuid)
    if protocol == GuacamoleBootstrapRequest.Protocol.NGFW_SSH:
        logger.info(
            "Guacamole SSH bootstrap queued for NGFW: user=%s ngfw_uuid=%s",
            _bootstrap_actor_fingerprint(user),
            safe_log_value(target_id),
        )
    authority_range = authority.get("authority_target_uuid")
    if authority_range is not None and not isinstance(authority_range, UUID):
        raise BootstrapFailure("Permission denied", status_code=403)
    build_url = _worker_build_callable(
        protocol=protocol,
        user=user,
        target_id=target_id,
        guac_client=guac_client,
        session_identity="access-" + str(bootstrap_id) if bootstrap_id else None,
        range_uuid=authority_range if protocol != "ngfw_ssh" else None,
    )
    try:
        bootstrap = enqueue_guacamole_bootstrap(
            user_id=None,
            protocol=protocol,
            target_id=target_id,
            build_url=build_url,
            authority=authority,
            guacamole_client=guac_client,
            bootstrap_id=bootstrap_id,
        )
    except BootstrapQueueFull:
        # Preserve the application-level saturation signal (the adapter maps this
        # to a 503 + Retry-After). Only non-secret kind/target/user identifiers.
        logger.warning(
            "Guacamole bootstrap worker capacity exhausted: user=%s protocol=%s target_id=%s",
            _bootstrap_actor_fingerprint(user),
            safe_log_value(protocol),
            safe_log_value(target_id),
        )
        raise
    return GuacamoleSessionLaunch(bootstrap_id=bootstrap.id, status=bootstrap.status)


def _bootstrap_actor_fingerprint(user: CredentialContext) -> str:
    """Keep session diagnostics bound to a nonsecret actor fingerprint."""
    return safe_log_fingerprint(str(user.principal.uuid))
