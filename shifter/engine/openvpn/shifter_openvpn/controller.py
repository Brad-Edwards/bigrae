"""Authorize, track and revoke the clients of one pool server.

* CONNECT: ask the portal; admit with a route to the single granted target, or
  refuse. A portal that cannot answer means refuse.
* ESTABLISHED: allow the client's tunnel address to the target's ports only.
* DISCONNECT: remove the allowance and tell the portal.
* Heartbeat: report live sessions; disconnect every session the portal returns.
  When the portal cannot be reached for longer than the grace period, every
  session ends: revocation that cannot be confirmed is not assumed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from .management import ClientEvent
from .portal import Grant, PortalRefused, PortalUnavailable

_VPN_TERMINATION_UNAVAILABLE = "VPN termination unavailable"


RETRY_SECONDS = 5
MAX_HEARTBEAT_SECONDS = 15
TERMINATION_BATCH_SECONDS = 10
_LOGGER = logging.getLogger(__name__)


class ManagementPort(Protocol):
    """The OpenVPN management commands the policy issues."""

    def allow(self, cid: int, kid: int, config_lines: list[str]) -> None: ...
    def allow_unchanged(self, cid: int, kid: int) -> None: ...
    def deny(self, cid: int, kid: int, reason: str) -> None: ...
    def kill(self, cid: int) -> None: ...


class PortalPort(Protocol):
    """The portal's session decisions."""

    def authorize(self, common_name: str, client_id: int, client_address: str, **certificate: str) -> Grant: ...
    def heartbeat(self, sessions: list[str]) -> tuple[set[str], int]: ...
    def end(self, session: str) -> None: ...


class FirewallPort(Protocol):
    """Per-client forwarding allowances."""

    def allow(self, client: str, target: str, ports: tuple[int, ...]) -> None: ...
    def revoke(self, client: str, target: str, ports: tuple[int, ...]) -> None: ...


class CrlPort(Protocol):
    """Local validation and installation of the issuer's signed list."""

    def install(self, pem: str) -> None: ...
    def valid(self) -> bool: ...


@dataclass
class Session:
    """One admitted client and what it may reach."""

    session_id: str
    target: str
    ports: tuple[int, ...]
    address: str | None = None


@dataclass(frozen=True)
class RevocationControl:
    """Local CRL state and the server's fail-closed recovery hooks."""

    store: CrlPort | None = None
    refresh: Callable[[], str] | None = None
    fatal: Callable[[], None] | None = None


class Controller:
    """The server-side policy for every client of this OpenVPN process."""

    def __init__(
        self,
        management: ManagementPort,
        portal: PortalPort,
        firewall: FirewallPort,
        *,
        grace_seconds: int = 90,
        clock: Callable[[], float] = time.monotonic,
        revocation: RevocationControl | None = None,
    ) -> None:
        self._management = management
        self._portal = portal
        self._firewall = firewall
        self._grace = grace_seconds
        self._clock = clock
        revocation = revocation or RevocationControl()
        self._crl, self._refresh_crl = revocation.store, revocation.refresh
        self._fatal = revocation.fatal or (lambda: None)
        self._lock = threading.Lock()
        self._sessions: dict[int, Session] = {}
        self._last_heartbeat = clock()
        self._authorizers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="authorize")

    def handle(self, event: ClientEvent) -> None:
        """Dispatch one management notification; authorization runs off the event thread."""
        if event.kind == "CONNECT":
            self._authorizers.submit(self._connect_safely, event)
        elif event.kind == "REAUTH":
            self._reauth(event)
        elif event.kind == "ESTABLISHED":
            self._established(event)
        elif event.kind == "DISCONNECT":
            self._disconnect(event)

    def _connect_safely(self, event: ClientEvent) -> None:
        try:
            self._connect(event)
        except Exception as error:  # noqa: BLE001 - fail closed; client and portal detail stay out of logs (class only)
            _LOGGER.error(
                "VPN authorization failed cid=%s exc_type=%s; stopping the server", event.cid, type(error).__name__
            )
            self._fatal()

    def _certificate_refusal(self, common_name: str) -> str:
        """Require valid local revocations for individual certificate identities."""
        individual = common_name.startswith("access-")
        if individual and (self._crl is None or not self._crl.valid()):
            return "vpn.crl_unavailable"
        if self._crl is not None and not individual:
            return "vpn.unknown_client"
        return ""

    def _connect(self, event: ClientEvent) -> None:
        kid = event.kid if event.kid is not None else 0
        common_name = event.env.get("common_name", "")
        refusal = self._certificate_refusal(common_name)
        if refusal:
            self._management.deny(event.cid, kid, refusal)
            return
        try:
            grant = self._portal.authorize(
                event.env.get("common_name", ""),
                event.cid,
                event.env.get("untrusted_ip", ""),
                certificate_serial=event.env.get("tls_serial_0", ""),
                certificate_fingerprint=event.env.get("tls_digest_sha256_0", ""),
            )
        except PortalRefused as refused:
            _LOGGER.info("deny cid=%s reason=%s", event.cid, refused.code)
            self._management.deny(event.cid, kid, refused.code)
            return
        except PortalUnavailable as unavailable:
            _LOGGER.warning("deny cid=%s reason=portal-unavailable detail=%s", event.cid, unavailable)
            self._management.deny(event.cid, kid, "portal unavailable")
            return
        with self._lock:
            self._sessions[event.cid] = Session(grant.session, grant.target, grant.ports)
        _LOGGER.info(
            "allow cid=%s session=%s target=%s ports=%s", event.cid, grant.session, grant.target, list(grant.ports)
        )
        self._management.allow(event.cid, kid, [f'push "route {grant.target} 255.255.255.255"'])

    def _reauth(self, event: ClientEvent) -> None:
        kid = event.kid if event.kid is not None else 0
        with self._lock:
            live = {cid: session.session_id for cid, session in self._sessions.items()}
        # Recheck the recorded session, without minting a replacement identity.
        if event.cid in live:
            try:
                disconnect, _interval = self._portal.heartbeat(sorted(live.values()))
            except (PortalRefused, PortalUnavailable):
                self._kill(event.cid, "reauth-unconfirmed")
                return
            for cid, session_id in live.items():
                if session_id in disconnect:
                    self._kill(cid, "revoked")
            with self._lock:
                still_known = event.cid in self._sessions and self._sessions[event.cid].session_id == live[event.cid]
            if still_known:
                self._management.allow_unchanged(event.cid, kid)
        else:
            self._management.deny(event.cid, kid, "unknown session")

    def _established(self, event: ClientEvent) -> None:
        address = event.env.get("ifconfig_pool_remote_ip", "")
        with self._lock:
            session = self._sessions.get(event.cid)
            if session is None or not address:
                self._management.kill(event.cid)
                return
            try:
                self._firewall.allow(address, session.target, session.ports)
            except Exception as error:  # noqa: BLE001 - the client is killed; firewall errors are logged by class only
                _LOGGER.error("kill cid=%s reason=firewall exc_type=%s", event.cid, type(error).__name__)
                self._management.kill(event.cid)
                return
            # Installation and address publication are fenced with retirement.
            session.address = address

    def _disconnect(self, event: ClientEvent) -> None:
        with self._lock:
            session = self._sessions.pop(event.cid, None)
            if session is None:
                return
            if session.address:
                try:
                    self._firewall.revoke(session.address, session.target, session.ports)
                except Exception as error:  # noqa: BLE001 - fail closed with the authored termination error; class-only log
                    _LOGGER.error("revoke failed cid=%s exc_type=%s", event.cid, type(error).__name__)
                    # Stale allowances could authorize a later address occupant.
                    raise RuntimeError(_VPN_TERMINATION_UNAVAILABLE) from None
        try:
            self._portal.end(session.session_id)
        except (PortalRefused, PortalUnavailable) as error:
            _LOGGER.warning(
                "end not recorded session=%s detail=%s; the heartbeat reconciles it", session.session_id, error
            )
        _LOGGER.info("disconnect cid=%s session=%s", event.cid, session.session_id)

    def _kill(self, cid: int, reason: str) -> None:
        _LOGGER.info("kill cid=%s reason=%s", cid, reason)
        with self._lock:
            session = self._sessions.pop(cid, None)
            if session is None:
                # A fenced disconnect or prior kill already retired this client.
                return
            # Removal is independent of the eventual DISCONNECT notification.
            forwarding_removed = session.address is None
            if session.address:
                try:
                    self._firewall.revoke(session.address, session.target, session.ports)
                    forwarding_removed = True
                except Exception as error:  # noqa: BLE001 - checked below: a kept allowance raises; class-only log
                    _LOGGER.error("revoke failed cid=%s exc_type=%s", cid, type(error).__name__)
        try:
            self._management.kill(cid)
        except Exception as error:  # noqa: BLE001 - fail closed with the authored termination error; class-only log
            _LOGGER.error("kill failed cid=%s exc_type=%s", cid, type(error).__name__)
            raise RuntimeError(_VPN_TERMINATION_UNAVAILABLE) from None
        if not forwarding_removed:
            raise RuntimeError(_VPN_TERMINATION_UNAVAILABLE)

    def heartbeat(self) -> int:
        """Renew live sessions, apply revocations, and return the seconds until the next beat."""
        with self._lock:
            live = {cid: session.session_id for cid, session in self._sessions.items()}
        if not self._current_crl():
            self._kill_many(live, "crl-unavailable")
            return RETRY_SECONDS
        try:
            disconnect, interval = self._portal.heartbeat(sorted(live.values()))
        except (PortalRefused, PortalUnavailable) as error:
            silent = self._clock() - self._last_heartbeat
            _LOGGER.warning("heartbeat failed detail=%s; %.0fs since the last confirmation", error, silent)
            if silent > self._grace:
                self._kill_many(live, "revocation-unconfirmed")
            return RETRY_SECONDS
        self._last_heartbeat = self._clock()
        self._kill_many([cid for cid, session in live.items() if session in disconnect], "revoked")
        return min(MAX_HEARTBEAT_SECONDS, max(RETRY_SECONDS, interval))

    def _kill_many(self, clients: Iterable[int], reason: str) -> None:
        deadline = self._clock() + TERMINATION_BATCH_SECONDS
        for cid in clients:
            self._kill(cid, reason)
            if self._clock() > deadline:
                # The native pool restart ends the remaining clients together.
                raise RuntimeError(_VPN_TERMINATION_UNAVAILABLE)

    def _current_crl(self) -> bool:
        if self._crl is None:
            return True
        if self._refresh_crl is not None:
            try:
                self._crl.install(self._refresh_crl())
            except (ValueError, PortalRefused, PortalUnavailable) as error:
                _LOGGER.warning("CRL refresh unavailable exc_type=%s", type(error).__name__)
        return self._crl.valid()

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)
