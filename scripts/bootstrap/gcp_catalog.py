"""Initialize shipped catalog content only with current native application proof."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import yaml
from installation.gcp_catalog import render_catalog_job

from bootstrap_core import success, warn
from gcp_identity_cutover import IdentityCutoverError, _apply, _delete, _run_job
from gcp_process import GcpCommandError, ProcessRunner


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
    bootstrap_facts: Path | None = None,
) -> None:
    """Register the in-box catalog after app readiness; never infer authority from IAM.

    A supplied credential acts as its own principal, and its failure stops the
    run. Otherwise the Job acts as the recorded bootstrap administrator named by
    ``bootstrap_facts`` (#2679). The application is already active, so a failed
    or skipped automatic registration is a warning with the next step.
    """
    if credential_file:
        _run_catalog_job(runner, documents, _read_credential(credential_file), bootstrap=False)
        return
    if bootstrap_facts is None:
        warn(_PENDING.format(hostname=hostname))
        return
    try:
        facts = Path(bootstrap_facts).read_text(encoding="utf-8")
        _run_catalog_job(runner, documents, facts, bootstrap=True)
    except (OSError, GcpCommandError, IdentityCutoverError) as exc:
        warn(
            f"Automatic catalog registration did not complete ({type(exc).__name__}). "
            + _PENDING.format(hostname=hostname)
        )
        return
    success("In-box catalog is registered")


_PENDING = (
    "The in-box catalog may be incomplete. Sign in at https://{hostname} as a native administrator, "
    "issue a content credential, and rerun this deployment with --catalog-credential-file <private 0600 file>."
)


def _run_catalog_job(
    runner: ProcessRunner, documents: Sequence[dict[str, object]], private_input: str, *, bootstrap: bool
) -> None:
    """Run one catalog Job with its private input mounted from a short-lived Secret."""
    name = f"catalog-initialize-{uuid4().hex[:12]}"
    job = render_catalog_job(
        yaml.safe_dump_all(documents, sort_keys=False), job_name=name, secret_name=name, bootstrap_facts=bootstrap
    )
    try:
        _apply(
            runner,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {"name": name, "namespace": "shifter-platform"},
                "stringData": {"credential": private_input},
            },
        )
        _run_job(runner, job, timeout="30m")
    finally:
        _delete(runner, "secret", name)
