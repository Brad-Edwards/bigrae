"""Explicit, synthetic qualification against the configured OpenVPN 2.6 image."""

import ipaddress
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from shifter_openvpn.config import Config
from shifter_openvpn.server_config import render

from tests.conftest import build_vpn_issuer_payload
from vpn_access import OpenVpnPoolEndpoint, build_signed_crl, load_issuer, mint_individual_profile


def test_native_openvpn_verified_identity_revocation_and_independent_client(tmp_path):
    image = os.environ.get("SHIFTER_VPN_NATIVE_IMAGE")
    if not image:
        pytest.skip("explicit local pool image required")
    issuer_payload = json.loads(build_vpn_issuer_payload())
    issuer_payload["tls_crypt"] = (
        "-----BEGIN OpenVPN Static key V1-----\n"
        + "\n".join(os.urandom(16).hex() for _ in range(16))
        + "\n-----END OpenVPN Static key V1-----\n"
    )
    issuer = load_issuer(json.dumps(issuer_payload))
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "shifter-openvpn-server")]))
        .issuer_name(issuer.ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(issuer.ca_private_key, hashes.SHA256())
    )
    material = {
        "ca.crt": issuer.ca_pem,
        "server.crt": certificate.public_bytes(serialization.Encoding.PEM).decode(),
        "server.key": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        "tls-crypt.key": issuer.tls_crypt,
        "initial-crl.pem": build_signed_crl(issuer, [], now=now, revision=1),
    }
    identities = {}
    for number in (1, 2):
        profile_id = uuid4()
        profile, serial, fingerprint = mint_individual_profile(
            issuer,
            generation=uuid4(),
            profile_id=profile_id,
            not_after=now + timedelta(minutes=30),
            endpoint=OpenVpnPoolEndpoint("198.18.0.1", 1194),
        )
        material[f"client-{number}.ovpn"] = profile
        identities[f"access-{profile_id}"] = {"serial": serial, "fingerprint": fingerprint}
        if number == 1:
            material["revoked-crl.pem"] = build_signed_crl(issuer, [(int(serial), now)], now=now, revision=2)
    config = Config(
        "https://portal.example.test",
        "synthetic",
        "projects/synthetic/secrets/server",
        "synthetic-vpn",
        ipaddress.ip_network("100.96.0.0/24"),
        10,
        8080,
        "/fixture",
        True,
    )
    material["server.conf"] = render(config).replace(
        "management-client-user " + __import__("pwd").getpwuid(os.getuid()).pw_name, "management-client-user root"
    )
    material["identities.json"] = json.dumps(identities)
    for name, value in material.items():
        path = tmp_path / name
        path.write_text(value)
        path.chmod(0o600)
    openvpn = Path(__file__).resolve().parents[2] / "openvpn"
    name = "shifter-vpn-pki-" + uuid4().hex[:10]
    try:
        result = subprocess.run(  # noqa: S603 - fixture-owned argv, no shell
            [
                "/usr/bin/docker",
                "run",
                "--name",
                name,
                "--user",
                "0",
                "--network",
                "none",
                "--env",
                "PYTHONPATH=/opt/shifter-openvpn",
                "--env",
                "SHIFTER_VPN_QUALIFY_OUTAGE=" + os.environ.get("SHIFTER_VPN_QUALIFY_OUTAGE", "0"),
                "--cap-add",
                "NET_ADMIN",
                "--cap-add",
                "SYS_ADMIN",
                "--device",
                "/dev/net/tun",
                "--security-opt",
                "apparmor=unconfined",
                "--security-opt",
                "seccomp=unconfined",
                "--sysctl",
                "net.ipv4.ip_forward=1",
                "--mount",
                f"type=bind,src={tmp_path},dst=/fixture",
                "--mount",
                f"type=bind,src={openvpn / 'shifter_openvpn'},dst=/opt/shifter-openvpn/shifter_openvpn,readonly",
                "--mount",
                f"type=bind,src={openvpn / 'tests'},dst=/qualification,readonly",
                "--entrypoint",
                "python3",
                image,
                "/qualification/native_pki_runner.py",
            ],
            capture_output=True,
            text=True,
            timeout=150,
        )
        server_log = tmp_path / "server.log"
        assert result.returncode == 0, (
            result.stdout + result.stderr + (server_log.read_text() if server_log.exists() else "")
        )
        assert "independent client passed" in result.stdout
    finally:
        subprocess.run(  # noqa: S603 - fixture-owned argv, no shell
            ["/usr/bin/docker", "rm", "-f", name], capture_output=True, timeout=10
        )
