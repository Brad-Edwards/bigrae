"""Exact-digest release scan gate for GCP deploys (#2571, ADR-037-R8).

The scan runs under the release-scan purpose identity only. For each exact image
identity it runs the pinned Trivy binary for HIGH and CRITICAL vulnerabilities, with the
repository ignore file and ``--exit-code 1``. Trivy reads its vulnerability databases
from Google's ``mirror.gcr.io`` mirror, never from GitHub. The raw reports and the release
manifest go to the private release-evidence bucket under ``release-scans/``. A finding,
a scanner failure, or an evidence-storage failure blocks the rollout.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bootstrap_core import error, info, success
from gcp_evidence import EvidenceRecord, evidence_prefix, store_evidence, store_verdict
from gcp_inputs import private_workdir, write_private_file
from gcp_process import GcpCommandError, ProcessRunner

TRIVY_DB_REPOSITORY = "mirror.gcr.io/aquasec/trivy-db"
TRIVY_JAVA_DB_REPOSITORY = "mirror.gcr.io/aquasec/trivy-java-db"
SEVERITIES = "HIGH,CRITICAL"


class ReleaseScanError(RuntimeError):
    """The exact-release scan found a blocking finding or could not complete."""


@dataclass(frozen=True)
class ScanOutcome:
    """The result of one exact-release scan."""

    passed: bool
    failed_images: tuple[str, ...]
    record: EvidenceRecord


def trivy_argv(trivy: str, identity: str, report: Path, ignore_file: Path) -> list[str]:
    """Return the fixed Trivy argv for one exact image identity."""
    return [
        trivy,
        "image",
        "--scanners",
        "vuln",
        "--severity",
        SEVERITIES,
        "--ignore-unfixed",
        "--ignorefile",
        str(ignore_file),
        "--db-repository",
        TRIVY_DB_REPOSITORY,
        "--java-db-repository",
        TRIVY_JAVA_DB_REPOSITORY,
        "--exit-code",
        "1",
        "--format",
        "json",
        "--output",
        str(report),
        identity,
    ]


def registry_access_token(runner: ProcessRunner, env: Mapping[str, str | None]) -> str:
    """Return a short-lived access token for the selected identity."""
    token = runner.run(["gcloud", "auth", "print-access-token"], env=env, sensitive=True).stdout.strip()
    if not token:
        raise ReleaseScanError("the scanner identity returned no access token")
    return token


def scan_release(
    runner: ProcessRunner,
    scan_env: Mapping[str, str | None],
    *,
    identities: Mapping[str, str],
    source_revision: str,
    evidence_bucket: str,
    run_id: str,
    attempt: int,
    workdir: Path,
    trivy: str = "trivy",
    ignore_file: Path,
) -> ScanOutcome:
    """Scan every exact identity, store the evidence, and fail closed on a finding."""
    results = private_workdir(workdir / "release-scan")
    manifest = {"schema_version": 2, "source_sha": source_revision, "images": dict(sorted(identities.items()))}
    files = [
        write_private_file(results / "release-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    ]
    token = registry_access_token(runner, scan_env)
    trivy_env = {**scan_env, "TRIVY_USERNAME": "oauth2accesstoken", "TRIVY_PASSWORD": token}
    failed: list[str] = []
    for name, identity in identities.items():
        report = results / f"{name}-trivy.json"
        info(f"Scanning {name} for {SEVERITIES} findings")
        result = runner.run(
            trivy_argv(trivy, identity, report, ignore_file), env=trivy_env, check=False, sensitive=True
        )
        if result.returncode != 0:
            failed.append(name)
        if report.is_file():
            report.chmod(0o600)
            files.append(report)
    prefix = evidence_prefix("release-scans", source_revision, run_id, attempt)
    record = store_evidence(runner, scan_env, bucket=evidence_bucket, prefix=prefix, files=files)
    outcome = "success" if not failed else "failure"
    store_verdict(
        runner,
        scan_env,
        bucket=evidence_bucket,
        record=record,
        source_revision=source_revision,
        result=outcome,
        workdir=results,
    )
    return ScanOutcome(passed=not failed, failed_images=tuple(failed), record=record)


def enforce_scan(outcome: ScanOutcome) -> None:
    """Stop the rollout unless every image passed."""
    if not outcome.passed:
        error("Exact-release scan found HIGH or CRITICAL findings in: " + ", ".join(outcome.failed_images))
        raise ReleaseScanError("exact-release scan failed; the rollout is blocked")
    success("Exact-release scan passed for every image")


def run_release_scan(
    runner: ProcessRunner,
    scan_env: Mapping[str, str | None],
    **kwargs: object,
) -> ScanOutcome:
    """Scan, store evidence, and enforce the gate in one call."""
    try:
        outcome = scan_release(runner, scan_env, **kwargs)  # type: ignore[arg-type]
    except GcpCommandError as exc:
        raise ReleaseScanError(f"exact-release scan could not complete: {exc}") from None
    enforce_scan(outcome)
    return outcome
