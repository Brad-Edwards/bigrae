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

RETRY_SECONDS = 5
MAX_HEARTBEAT_SECONDS = 15
TERMINATION_BATCH_SECONDS = 10
_LOGGER = logging.getLogger("shifter_openvpn")


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
        log: Callable[[str], None] | None = None,
        crl: CrlPort | None = None,
        refresh_crl: Callable[[], str] | None = None,
        fatal: Callable[[], None] | None = None,
    ) -> None:
        self._management = management
        self._portal = portal
        self._firewall = firewall
        self._grace = grace_seconds
        self._clock = clock
        self._log = log or _LOGGER.info
        self._crl, self._refresh_crl = crl, refresh_crl
        self._fatal = fatal or (lambda: None)
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
        except Exception as error:
            self._log(f"VPN authorization failed ({type(error).__name__}); stopping the server")
            self._fatal()

    def _connect(self, event: ClientEvent) -> None:
        kid = event.kid if event.kid is not None else 0
        common_name = event.env.get("common_name", "")
        if common_name.startswith("access-") and (self._crl is None or not self._crl.valid()):
            self._management.deny(event.cid, kid, "vpn.crl_unavailable")
            return
        if self._crl is not None and not common_name.startswith("access-"):
            self._management.deny(event.cid, kid, "vpn.unknown_client")
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
            self._log(f"deny cid={event.cid} reason={refused.code}")
            self._management.deny(event.cid, kid, refused.code)
            return
        except PortalUnavailable as unavailable:
            self._log(f"deny cid={event.cid} reason=portal-unavailable ({unavailable})")
            self._management.deny(event.cid, kid, "portal unavailable")
            return
        with self._lock:
            self._sessions[event.cid] = Session(grant.session, grant.target, grant.ports)
        self._log(f"allow cid={event.cid} session={grant.session} target={grant.target} ports={list(grant.ports)}")
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
            except Exception as error:
                self._log(f"kill cid={event.cid} reason=firewall ({type(error).__name__})")
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
                except Exception as error:
                    self._log(f"revoke failed cid={event.cid} ({type(error).__name__})")
                    # Stale allowances could authorize a later address occupant.
                    raise RuntimeError("VPN termination unavailable") from None
        try:
            self._portal.end(session.session_id)
        except (PortalRefused, PortalUnavailable) as error:
            self._log(f"end not recorded session={session.session_id} ({error}); the heartbeat reconciles it")
        self._log(f"disconnect cid={event.cid} session={session.session_id}")

    def _kill(self, cid: int, reason: str) -> None:
        self._log(f"kill cid={cid} reason={reason}")
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
                except Exception as error:
                    self._log(f"revoke failed cid={cid} ({type(error).__name__})")
        try:
            self._management.kill(cid)
        except Exception as error:
            self._log(f"kill failed cid={cid} ({type(error).__name__})")
            raise RuntimeError("VPN termination unavailable") from None
        if not forwarding_removed:
            raise RuntimeError("VPN termination unavailable")

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
            self._log(f"heartbeat failed ({error}); {silent:.0f}s since the last confirmation")
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
                raise RuntimeError("VPN termination unavailable")

    def _current_crl(self) -> bool:
        if self._crl is None:
            return True
        if self._refresh_crl is not None:
            try:
                self._crl.install(self._refresh_crl())
            except (ValueError, PortalRefused, PortalUnavailable):
                self._log("CRL refresh unavailable")
        return self._crl.valid()

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)
