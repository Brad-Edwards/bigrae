"""Initialize shipped catalog content only with current native application proof."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import yaml
from installation.gcp_catalog import render_catalog_job

from gcp_identity_cutover import IdentityCutoverError, _apply, _delete, _run_job
from gcp_process import ProcessRunner


def _read_credential(path: str | Path) -> str:
    """Read a bounded bearer from a private regular file without following links."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ValueError
            payload = handle.read(16_385)
        credential = payload.decode("utf-8").strip()
        if not credential or len(payload) > 16_384 or any(c.isspace() for c in credential) or "\x00" in credential:
            raise ValueError
    except (OSError, ValueError):
        raise IdentityCutoverError(
            "Catalog credential must be a regular private 0600 bearer file, at most 16KiB"
        ) from None
    return credential


def validate_catalog_credential_path(path: str | Path | None) -> None:
    """Validate supplied proof before deployment effects; absence permits app recovery."""
    if path:
        _read_credential(path)


def initialize_catalog(
    runner: ProcessRunner,
    documents: Sequence[dict[str, object]],
    *,
    credential_file: str | Path | None,
    hostname: str,
) -> None:
    """Run after app readiness and DNS instructions; never infer authority from IAM."""
    if not credential_file:
        raise IdentityCutoverError(
            f"Native application is active; catalog setup is incomplete. Finish DNS/TLS for https://{hostname}, "
            "sign in as a native administrator and obtain a current content credential. "
            "Rerun this deployment with --catalog-credential-file <private 0600 bearer file>."
        )
    credential = _read_credential(credential_file)
    name = f"catalog-initialize-{uuid4().hex[:12]}"
    job = render_catalog_job(yaml.safe_dump_all(documents, sort_keys=False), job_name=name, secret_name=name)
    try:
        _apply(
            runner,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {"name": name, "namespace": "shifter-platform"},
                "stringData": {"credential": credential},
            },
        )
        _run_job(runner, job, timeout="30m")
    finally:
        _delete(runner, "secret", name)
