"""OpenFGA datastore rotation package build (#2313)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

import openfga_rotation_package

CA = b"-----BEGIN CERTIFICATE-----\nU3ludGhldGljIFJEUyBDQQ==\n-----END CERTIFICATE-----\n"


class _Connection:
    """The HTTPS trust-store endpoint with a fixed response body and status."""

    requests: ClassVar[list[tuple[str, str, str]]] = []

    def __init__(self, host: str, timeout: int, body: bytes = CA, status: int = 200) -> None:
        self.host, self.timeout, self.body, self.status = host, timeout, body, status

    def request(self, method: str, path: str) -> None:
        self.requests.append((self.host, method, path))

    def getresponse(self):
        return self

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


def test_rds_ca_bundle_comes_only_from_the_aws_trust_store(monkeypatch):
    _Connection.requests = []
    monkeypatch.setattr(openfga_rotation_package.http.client, "HTTPSConnection", _Connection)
    assert openfga_rotation_package.fetch_rds_ca_bundle("us-east-2") == CA
    assert _Connection.requests == [("truststore.pki.rds.amazonaws.com", "GET", "/us-east-2/us-east-2-bundle.pem")]
    for region in ("us-east-2/../x", "evil.example", ""):
        with pytest.raises(ValueError):
            openfga_rotation_package.fetch_rds_ca_bundle(region)
    for body, status in ((b"<html>not a bundle</html>", 200), (CA * 20_000, 200), (CA, 404)):
        monkeypatch.setattr(
            openfga_rotation_package.http.client,
            "HTTPSConnection",
            lambda host, timeout, b=body, s=status: _Connection(host, timeout, b, s),
        )
        with pytest.raises(ValueError):
            openfga_rotation_package.fetch_rds_ca_bundle("us-east-2")


def test_rotation_package_is_hash_pinned_and_deterministic(tmp_path, monkeypatch):
    calls = []

    def run_cmd(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["uv", "export"]:
            Path(argv[argv.index("--output-file") + 1]).write_text("psycopg==3.3.4 --hash=sha256:synthetic\n")
            return
        target = Path(argv[argv.index("--target") + 1])
        (target / "psycopg").mkdir()
        (target / "psycopg" / "__init__.py").write_text("# synthetic\n")

    monkeypatch.setattr(openfga_rotation_package, "run_cmd", run_cmd)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = openfga_rotation_package.build_rotation_package(tmp_path / "a", CA)
    second = openfga_rotation_package.build_rotation_package(tmp_path / "b", CA)
    assert first.read_bytes() == second.read_bytes()
    export, install = calls[0], calls[1]
    # The frozen uv.lock is the only pin source; the install checks every hash.
    assert export[:2] == ["uv", "export"] and {"--frozen", "--no-dev", "--no-emit-project"} <= set(export)
    assert export[export.index("--project") + 1].endswith("eks/lambda")
    assert {"--require-hashes", "--no-deps", "--only-binary"} <= set(install)
    assert install[install.index("-r") + 1] == export[export.index("--output-file") + 1]
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert {"openfga_rotation.py", "rds-ca-bundle.pem", "psycopg/__init__.py"} <= set(names)
        assert archive.read("rds-ca-bundle.pem") == CA
