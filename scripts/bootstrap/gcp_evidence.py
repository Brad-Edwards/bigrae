"""Private release evidence for the GCP deploy commands (#2571, ADR-037-R8).

Raw evidence (exact-digest scan reports, the running-image inventory) goes only to the
deployment's private release-evidence bucket, under the prefix that the producing
purpose identity may create: ``release-scans/`` for the scanner and ``deployments/``
for the deployer. A redacted verdict with an opaque locator and the evidence digest is
stored next to it. The layout and digests match the earlier CI evidence, so existing
evidence readers keep working.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gcp_inputs import write_private_file
from gcp_process import GcpCommandError, ProcessRunner

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
EvidenceClass = str  # "release-scans" or "deployments"
_EVIDENCE_CLASSES = ("release-scans", "deployments")


@dataclass(frozen=True)
class EvidenceRecord:
    """The private location and digest of one stored evidence set."""

    prefix: str
    locator: str
    evidence_sha256: str


def evidence_prefix(evidence_class: EvidenceClass, source_revision: str, run_id: str, attempt: int) -> str:
    """Return the object prefix of one evidence set."""
    if evidence_class not in _EVIDENCE_CLASSES:
        raise ValueError(f"unsupported evidence class {evidence_class!r}")
    if not _REVISION_RE.fullmatch(source_revision):
        raise ValueError("evidence requires a full 40-character source revision")
    if not _RUN_ID_RE.fullmatch(run_id) or attempt < 1:
        raise ValueError("evidence requires a bounded run id and a positive attempt")
    return f"{evidence_class}/{source_revision}/{run_id}/{attempt}"


def evidence_digest(files: Sequence[Path]) -> str:
    """Digest a file set exactly as ``sha256sum ./*.json | sort | sha256sum`` does."""
    lines = sorted(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.name}\n" for path in files)
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def store_evidence(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    bucket: str,
    prefix: str,
    files: Sequence[Path],
) -> EvidenceRecord:
    """Copy raw evidence files to the private bucket and return the record."""
    if not _BUCKET_RE.fullmatch(bucket):
        raise ValueError("release evidence bucket name is invalid")
    if not files:
        raise ValueError("evidence requires at least one file")
    for path in files:
        try:
            runner.run(
                ["gcloud", "storage", "cp", "--quiet", str(path), f"gs://{bucket}/{prefix}/{path.name}"], env=env
            )
        except GcpCommandError:
            raise GcpCommandError("failed to store raw release evidence privately") from None
    return EvidenceRecord(
        prefix=prefix,
        locator=hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        evidence_sha256=evidence_digest(files),
    )


def store_verdict(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    bucket: str,
    record: EvidenceRecord,
    source_revision: str,
    result: str,
    workdir: Path,
) -> Path:
    """Write and store the redacted verdict next to its evidence."""
    verdict = {
        "schema_version": 1,
        "source_sha": source_revision,
        "result": result,
        "evidence_locator": record.locator,
        "evidence_sha256": record.evidence_sha256,
    }
    path = write_private_file(workdir / "verdict.json", json.dumps(verdict, sort_keys=True) + "\n")
    try:
        runner.run(
            ["gcloud", "storage", "cp", "--quiet", str(path), f"gs://{bucket}/{record.prefix}/verdict.json"], env=env
        )
    except GcpCommandError:
        raise GcpCommandError("failed to store the redacted release verdict") from None
    return path
