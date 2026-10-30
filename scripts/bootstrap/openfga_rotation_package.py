"""OpenFGA datastore rotation package for the EKS Terraform root (#2313).

The deploy builds the package from the hash-pinned lock, the rotation function
and the region's public RDS CA bundle. Terraform uploads it, Signer signs it,
and the functions deploy the signed copy from S3, so teardown needs no package.
"""

from __future__ import annotations

import http.client
import re
import shutil
import zipfile
from pathlib import Path

from bootstrap_core import get_repo_root, run_cmd

RDS_CA_HOST = "truststore.pki.rds.amazonaws.com"
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d+", flags=re.ASCII)
_MAX_CA_BYTES = 262_144
_ROTATION_SOURCE = Path("platform/terraform/modules/portal/eks/lambda")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def package_path(workdir: Path) -> Path:
    """Return where the package for this work directory is written."""
    return workdir / "openfga-rotation.zip"


def fetch_rds_ca_bundle(region: str) -> bytes:
    """Return the region's public RDS CA bundle from the official AWS trust store."""
    if not _REGION.fullmatch(region):
        raise ValueError("RDS CA bundle requires a valid AWS region")
    # HTTPSConnection verifies the trust-store certificate with the default context.
    connection = http.client.HTTPSConnection(RDS_CA_HOST, timeout=30)
    try:
        connection.request("GET", f"/{region}/{region}-bundle.pem")
        response = connection.getresponse()
        bundle = response.read(_MAX_CA_BYTES + 1)
        status = response.status
    finally:
        connection.close()
    if status != 200 or len(bundle) > _MAX_CA_BYTES or b"-----BEGIN CERTIFICATE-----" not in bundle:
        raise ValueError("RDS CA bundle download is not a bounded PEM certificate bundle")
    return bundle


def build_rotation_package(workdir: Path, ca_bundle: bytes) -> Path:
    """Build the rotation Lambda zip from the hash-pinned lock and the RDS CA bundle.

    The zip is deterministic: sorted entries, a fixed timestamp and fixed modes,
    so an unchanged lock and bundle produce the same Terraform source hash.
    """
    source = get_repo_root() / _ROTATION_SOURCE
    staging = workdir / "openfga-rotation"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # uv.lock is the only pin source; the export carries its hashes and markers.
    requirements = workdir / "openfga-rotation-requirements.txt"
    run_cmd(
        [
            "uv",
            "export",
            "--quiet",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--project",
            str(source),
            "--output-file",
            str(requirements),
        ]
    )
    run_cmd(
        [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--python-version",
            "3.12",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--target",
            str(staging),
            "-r",
            str(requirements),
        ]
    )
    shutil.copy2(source / "openfga_rotation.py", staging / "openfga_rotation.py")
    (staging / "rds-ca-bundle.pem").write_bytes(ca_bundle)
    package = package_path(workdir)
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in staging.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            info = zipfile.ZipInfo(str(path.relative_to(staging)), date_time=_ZIP_TIMESTAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    shutil.rmtree(staging, ignore_errors=True)
    return package
