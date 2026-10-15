"""Qualify established-flow revocation with native nftables in isolated namespaces.

Run explicitly with SHIFTER_VPN_NATIVE_IMAGE set to the locally built pool image.
The fixture has no Docker network, published ports, or external connectivity.
"""

from __future__ import annotations

import ipaddress
import os
import select
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from shifter_openvpn.firewall import base_ruleset


def test_revocation_stops_an_established_tcp_connection():
    image = os.environ.get("SHIFTER_VPN_NATIVE_IMAGE")
    if not image:
        pytest.skip("explicit local pool image required")
    name = "shifter-vpn-native-" + uuid4().hex[:10]
    source = Path(__file__).resolve().parents[1] / "shifter_openvpn"

    def docker(*args, input=None):
        result = subprocess.run(  # noqa: S603 - fixture-owned argv, no shell
            ["/usr/bin/docker", *args], input=input, text=True, capture_output=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def execute(*args, input=None):
        return docker("exec", "-i", name, *args, input=input)

    try:
        # SYS_ADMIN allows ip netns to mount handles inside this fixture only;
        # neither the host network nor PID namespaces are shared.
        docker(
            "run",
            "-d",
            "--user",
            "0",
            "--network",
            "none",
            "--name",
            name,
            "--cap-add",
            "NET_ADMIN",
            "--cap-add",
            "SYS_ADMIN",
            "--security-opt",
            "apparmor=unconfined",
            "--security-opt",
            "seccomp=unconfined",
            "--sysctl",
            "net.ipv4.ip_forward=1",
            "--mount",
            f"type=bind,src={source},dst=/opt/shifter-openvpn/shifter_openvpn,readonly",
            "--entrypoint",
            "sleep",
            image,
            "300",
        )
        for namespace, interface, network in (("client", "tun0", "198.18.0"), ("target", "backend0", "198.19.0")):
            execute("ip", "netns", "add", namespace)
            execute("ip", "link", "add", interface, "type", "veth", "peer", "name", namespace + "0")
            execute("ip", "link", "set", namespace + "0", "netns", namespace)
            execute("ip", "addr", "add", network + ".1/30", "dev", interface)
            execute("ip", "link", "set", interface, "up")
            execute("ip", "netns", "exec", namespace, "ip", "addr", "add", network + ".2/30", "dev", namespace + "0")
            execute("ip", "netns", "exec", namespace, "ip", "link", "set", namespace + "0", "up")
            execute("ip", "netns", "exec", namespace, "ip", "route", "add", "default", "via", network + ".1")
        execute("nft", "-f", "-", input=base_ruleset(ipaddress.ip_network("198.18.0.0/30")))
        docker(
            "exec",
            "-d",
            name,
            "ip",
            "netns",
            "exec",
            "target",
            "python3",
            "-c",
            "import socket; s=socket.create_server(('0.0.0.0',22)); c,_=s.accept(); "
            "exec('while True:\\n b=c.recv(1)\\n if not b: break\\n c.sendall(b)')",
        )
        execute(
            "python3",
            "-c",
            "from shifter_openvpn.firewall import Firewall; Firewall().allow('198.18.0.2','198.19.0.2',(22,))",
        )
        probe = subprocess.Popen(  # noqa: S603 - fixture-owned argv, no shell
            [
                "/usr/bin/docker",
                "exec",
                "-i",
                name,
                "ip",
                "netns",
                "exec",
                "client",
                "python3",
                "-u",
                "-c",
                "import socket,sys; s=socket.create_connection(('198.19.0.2',22),timeout=2); "
                "s.sendall(b'a'); assert s.recv(1)==b'a'; print('connected',flush=True); "
                "sys.stdin.readline(); s.sendall(b'b'); "
                "exec('try:\\n print(repr(s.recv(1)),flush=True)\\n"
                "except TimeoutError:\\n print(\"blocked\",flush=True)')",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert select.select([probe.stdout], [], [], 10)[0], "TCP probe did not connect"
            if probe.stdout.readline().strip() != "connected":
                _, errors = probe.communicate(timeout=5)
                pytest.fail(errors)
            execute(
                "python3",
                "-c",
                "from shifter_openvpn.firewall import Firewall; Firewall().revoke('198.18.0.2','198.19.0.2',(22,))",
            )
            output, errors = probe.communicate("continue\n", timeout=10)
            assert probe.returncode == 0, errors
            assert output.strip() == "blocked", "established TCP flow survived access revocation"
        finally:
            if probe.poll() is None:
                probe.kill()
                probe.communicate()
    finally:
        subprocess.run(  # noqa: S603 - fixture-owned argv, no shell
            ["/usr/bin/docker", "rm", "-f", name], capture_output=True, timeout=30
        )
