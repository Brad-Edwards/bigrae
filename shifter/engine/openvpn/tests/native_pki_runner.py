"""Native OpenVPN qualification runner; invoked inside an isolated local fixture."""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import subprocess
import time
from pathlib import Path

from shifter_openvpn.__main__ import _wait_for_socket
from shifter_openvpn.controller import Controller, RevocationControl
from shifter_openvpn.crl import CrlStore
from shifter_openvpn.firewall import Firewall
from shifter_openvpn.management import Management
from shifter_openvpn.portal import Grant, PortalUnavailable

ROOT = Path("/fixture")


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)  # noqa: S603 - fixed fixture commands


def wait_until(predicate, *, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("native OpenVPN condition did not arrive")


def setup_namespaces():
    for namespace, interface, network in (("client", "uplink0", "198.18.0"), ("target", "backend0", "198.19.0")):
        command("/sbin/ip", "netns", "add", namespace)
        command("/sbin/ip", "link", "add", interface, "type", "veth", "peer", "name", namespace + "0")
        command("/sbin/ip", "link", "set", namespace + "0", "netns", namespace)
        command("/sbin/ip", "addr", "add", network + ".1/30", "dev", interface)
        command("/sbin/ip", "link", "set", interface, "up")
        command("/sbin/ip", "netns", "exec", namespace, "ip", "addr", "add", network + ".2/30", "dev", namespace + "0")
        command("/sbin/ip", "netns", "exec", namespace, "ip", "link", "set", namespace + "0", "up")
        command("/sbin/ip", "netns", "exec", namespace, "ip", "route", "add", "default", "via", network + ".1")


def pump(management, controller, kind, *, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            event = management.events.get(timeout=0.1)
        except queue.Empty:
            continue
        assert event is not None, "management socket closed"
        controller.handle(event)
        if event.kind == kind:
            return event
    raise AssertionError(f"native {kind} notification did not arrive")


def qualify_outage(controller, management, portal):
    if os.environ.get("SHIFTER_VPN_QUALIFY_OUTAGE") == "1":
        controller.heartbeat()
        started = time.monotonic()
        portal.unavailable = True
        while controller.session_count():
            controller.heartbeat()
            assert time.monotonic() - started < 100, "native outage termination exceeded its fixture bound"
            time.sleep(0.25)
        pump(management, controller, "DISCONNECT", seconds=5)
        elapsed = time.monotonic() - started
        assert 90 <= elapsed < 100
        print(f"native outage disconnect after {elapsed:.2f} seconds", flush=True)  # noqa: T201


def run():
    setup_namespaces()
    identities = json.loads((ROOT / "identities.json").read_text())

    class Portal:
        def __init__(self):
            self.revoked = False
            self.unavailable = False
            self.observed = []

        def authorize(self, common_name, cid, address, **certificate):
            identity = identities[common_name]
            assert certificate["certificate_serial"] == identity["serial"]
            assert certificate["certificate_fingerprint"].replace(":", "").lower() == identity["fingerprint"]
            self.observed.append(common_name)
            return Grant(common_name, "198.19.0.2", (22,), 15)

        def heartbeat(self, sessions):
            if self.unavailable:
                raise PortalUnavailable("synthetic control outage")
            return (set(sessions) if self.revoked else set(), 15)

        def end(self, session):
            pass

    firewall = Firewall()
    firewall.install(ipaddress.ip_network("100.96.0.0/24"))
    crl = CrlStore(str(ROOT / "ca.crt"), str(ROOT / "crl.pem"))
    crl.install((ROOT / "initial-crl.pem").read_text())
    server_log = (ROOT / "server.log").open("w")
    server = subprocess.Popen(  # noqa: S603 - fixture-owned argv, no shell
        ["/usr/sbin/openvpn", "--config", str(ROOT / "server.conf")], stdout=server_log, stderr=server_log
    )
    client = echo = None
    try:
        _wait_for_socket(str(ROOT / "management.sock"), server, timeout=10)
        management = Management(str(ROOT / "management.sock"))
        portal = Portal()
        controller = Controller(management, portal, firewall, revocation=RevocationControl(crl))

        def start_client(number):
            log = ROOT / f"client-{number}.log"
            process = subprocess.Popen(  # noqa: S603 - fixture-owned argv, no shell
                [
                    "/sbin/ip",
                    "netns",
                    "exec",
                    "client",
                    "/usr/sbin/openvpn",
                    "--config",
                    str(ROOT / f"client-{number}.ovpn"),
                    "--log",
                    str(log),
                    "--connect-retry",
                    "1",
                    "--connect-retry-max",
                    "1",
                ]
            )
            return process, log

        # The real TLS handshake supplies the exact verified serial and digest.
        client, client_log = start_client(1)
        pump(management, controller, "ESTABLISHED")
        wait_until(lambda: "Initialization Sequence Completed" in client_log.read_text())
        echo = subprocess.Popen(
            [
                "/sbin/ip",
                "netns",
                "exec",
                "target",
                "python3",
                "-c",
                "import socket; s=socket.create_server(('0.0.0.0',22)); c,_=s.accept(); "
                "exec('while True:\\n b=c.recv(1)\\n if not b: break\\n c.sendall(b)')",
            ]
        )
        result = command(
            "/sbin/ip",
            "netns",
            "exec",
            "client",
            "python3",
            "-c",
            "import socket; s=socket.create_connection(('198.19.0.2',22),timeout=2); "
            "s.sendall(b'a'); assert s.recv(1)==b'a'",
        )
        assert result.returncode == 0
        portal.revoked = True
        crl.install((ROOT / "revoked-crl.pem").read_text())
        controller.heartbeat()
        disconnected = pump(management, controller, "DISCONNECT")
        assert disconnected.cid >= 0 and controller.session_count() == 0
        client.terminate()
        client.wait(timeout=5)

        # OpenVPN itself rejects the revoked certificate before CONNECT policy.
        before = len(portal.observed)
        client, _ = start_client(1)
        wait_until(lambda: "certificate revoked" in (ROOT / "server.log").read_text())
        assert len(portal.observed) == before
        client.terminate()
        client.wait(timeout=5)

        # A different credential's unrevoked certificate still connects.
        portal.revoked = False
        client, client_log = start_client(2)
        pump(management, controller, "ESTABLISHED")
        wait_until(lambda: "Initialization Sequence Completed" in client_log.read_text())
        assert len(portal.observed) == 2
        assert portal.observed[0] != portal.observed[1]
        qualify_outage(controller, management, portal)
        print(  # noqa: T201 - qualification result
            "native verified identity, client-kill, CRL reconnect rejection and independent client passed", flush=True
        )
    finally:
        for process in (client, echo, server):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        server_log.close()


if __name__ == "__main__":
    run()
