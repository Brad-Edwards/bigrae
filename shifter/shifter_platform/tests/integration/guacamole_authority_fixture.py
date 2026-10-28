"""Disposable control and Google metadata boundaries for local native tests."""

import base64
import json
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from shutil import which
from urllib.parse import parse_qs, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@contextmanager
def authority_control(gateway, directory):
    """Replace provider boundaries only, retaining the real Google token SDK."""
    audience = "https://portal.synthetic.test/guacamole-control"

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    token = ".".join(
        [
            encode({"alg": "RS256"}),
            encode(
                {
                    "aud": audience,
                    "sub": "123456789012345678901",
                    "email": "gateway@synthetic.iam.gserviceaccount.com",
                    "exp": int(time.time()) + 3600,
                }
            ),
            "synthetic-signature",
        ]
    )
    bindings = {}
    calls = []

    class Metadata(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers.get("Metadata-Flavor") == "Google"
            assert self.path.startswith("/computeMetadata/v1/instance/service-accounts/default/identity?")
            query = parse_qs(urlparse(self.path).query)
            assert query.get("audience") == [audience]
            assert query.get("format") == ["full"]
            raw = token.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Metadata-Flavor", "Google")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    class Control(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers.get("Authorization") == "Bearer " + token
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(body) == {"bootstrap"}
            identifier = body["bootstrap"]
            calls.append(identifier)
            state = bindings.get(identifier, "deny")
            raw = json.dumps({"allowed": state == "allow"}).encode()
            self.send_response(503 if state == "outage" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_):
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-control")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address(gateway))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "control.pem"
    key_path = directory / "control-key.pem"
    truststore = directory / "control.jks"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    executable = which("keytool")
    assert executable
    subprocess.run(  # noqa: S603 # nosec B603 - fixed executable and disposable fixture files
        [
            executable,
            "-importcert",
            "-noprompt",
            "-alias",
            "synthetic",
            "-file",
            str(certificate_path),
            "-keystore",
            str(truststore),
            "-storepass",
            "synthetic-test-only",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    metadata = ThreadingHTTPServer((gateway, 0), Metadata)
    control = ThreadingHTTPServer((gateway, 0), Control)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(certificate_path, key_path)
    control.socket = tls.wrap_socket(control.socket, server_side=True)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (metadata, control)]
    for thread in threads:
        thread.start()
    try:
        yield {
            "url": f"https://{gateway}:{control.server_port}/authority",
            "audience": audience,
            "metadata": f"{gateway}:{metadata.server_port}",
            "truststore": truststore,
            "bindings": bindings,
            "calls": calls,
        }
    finally:
        for server in (metadata, control):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
