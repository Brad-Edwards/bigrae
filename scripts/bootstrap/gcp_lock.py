"""Per-deployment lock in the deployment config bucket (#2571, ADR-070).

Only one deploy or destroy may run at a time for a deployment. The lock is one object,
``locks/deploy.lock``, created with the generation precondition ``0`` (create only when
absent) and deleted with a precondition on its own generation. A held lock stops the
command and names the holder. CI does not use this lock, because the workflow
concurrency group already queues deploys per deployment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gcp_inputs import LOCK_OBJECT, gcs_object_url, write_private_file
from gcp_process import GcpCommandError, ProcessRunner

_GENERATION_RE = re.compile(r"^[0-9]+$")
_PRECONDITION_MARKERS = ("412", "precondition", "conditionnotmet")


class DeployLockHeldError(RuntimeError):
    """Another deploy or destroy holds the deployment lock."""


@dataclass(frozen=True)
class DeployLock:
    """A held deployment lock and the generation that owns it."""

    url: str
    generation: str


def lock_url(store: str) -> str:
    """Return the lock object URL for a deployment config bucket prefix."""
    return gcs_object_url(store, LOCK_OBJECT)


def _describe_holder(runner: ProcessRunner, url: str, env: Mapping[str, str | None]) -> str:
    try:
        payload = json.loads(runner.run(["gcloud", "storage", "cat", url], env=env).stdout)
    except (GcpCommandError, json.JSONDecodeError):
        return "an unknown holder"
    if not isinstance(payload, dict):
        return "an unknown holder"
    holder = str(payload.get("command", "unknown"))
    started = str(payload.get("started_at", "unknown"))
    run_id = str(payload.get("run_id", "unknown"))
    return f"{holder} run {run_id} (started {started})"


def acquire_lock(
    runner: ProcessRunner,
    store: str,
    env: Mapping[str, str | None],
    *,
    command: str,
    run_id: str,
    workdir: Path,
    now: datetime | None = None,
) -> DeployLock:
    """Create the lock object or fail with the current holder."""
    url = lock_url(store)
    started = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = json.dumps({"command": command, "run_id": run_id, "started_at": started}, sort_keys=True)
    local = write_private_file(workdir / "deploy.lock", body + "\n")
    result = runner.run(
        ["gcloud", "storage", "cp", "--quiet", "--if-generation-match=0", str(local), url],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}".lower()
        if any(marker in detail for marker in _PRECONDITION_MARKERS):
            holder = _describe_holder(runner, url, env)
            raise DeployLockHeldError(
                f"the deployment lock is held by {holder}; wait for it to finish, or remove a stale lock "
                "with --break-lock"
            )
        raise GcpCommandError(f"cannot create the deployment lock (exit status {result.returncode})")
    generation = runner.run(
        ["gcloud", "storage", "objects", "describe", url, "--format=value(generation)"], env=env
    ).stdout.strip()
    if not _GENERATION_RE.fullmatch(generation):
        raise GcpCommandError("the deployment lock has no readable generation")
    return DeployLock(url=url, generation=generation)


def release_lock(runner: ProcessRunner, lock: DeployLock, env: Mapping[str, str | None]) -> None:
    """Delete the lock only when this run still owns its generation."""
    runner.run(
        ["gcloud", "storage", "rm", "--quiet", f"--if-generation-match={lock.generation}", lock.url],
        env=env,
    )


def break_lock(runner: ProcessRunner, store: str, env: Mapping[str, str | None]) -> bool:
    """Remove a stale lock. Return True when a lock existed."""
    url = lock_url(store)
    result = runner.run(["gcloud", "storage", "rm", "--quiet", url], env=env, check=False)
    return result.returncode == 0
