"""One identity cutover boundary inside the canonical GCP rollout.

Only chart prerequisites precede a conclusive status probe. Legacy installations
then stop effect producers before migration; full release restoration follows
verified native activation. Facts travel as private files and Secret stdin only.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import yaml
from installation.gcp_identity_cutover import probe_prerequisites, quiesce, render

from bootstrap_core import get_repo_root
from gcp_inputs import write_private_file
from gcp_process import ProcessRunner, script_argv

_NAMESPACE = "shifter-platform"
_RESULT_PREFIX = "SHIFTER_IDENTITY_CUTOVER_RESULT="
_LEGACY_STATES = {"absent", "planned", "sql_applied", "policy_pending"}
_FACT_SECRETS = ("identity-cutover-facts", "identity-bootstrap-facts")
# The owning identity commands bound both ceremonies to the same 64 KiB JSON
# contract. Refuse oversize inputs here before maintenance or migration starts.
_MAX_FACTS_BYTES = 65_536


class IdentityCutoverError(RuntimeError):
    """A bounded refusal to migrate or restore an unverified installation."""


@dataclass(frozen=True)
class CutoverRun:
    """The exact chart projection whose maintenance and activation are coupled."""

    required: bool
    manifest: str = field(repr=False)
    # The deploy generated the facts for an empty installation (#2679).
    fresh_install: bool = False
    bootstrap_facts: Path | None = None

    def with_documents(self, documents: Sequence[dict[str, object]]) -> CutoverRun:
        """Return the run bound to a re-rendered release, for example with the published policy IDs."""
        return CutoverRun(
            self.required,
            yaml.safe_dump_all(documents, sort_keys=False),
            fresh_install=self.fresh_install,
            bootstrap_facts=self.bootstrap_facts,
        )


#: Generates the cutover and bootstrap facts files of an empty installation.
FreshInstallFacts = Callable[[], tuple[Path, Path]]


def _unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON fields before retaining explicit operator facts."""
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise IdentityCutoverError("Identity facts contain duplicate fields")
        result[key] = value
    return result


def _read_facts(path: Path) -> dict[str, object]:
    """Read bounded private JSON from a regular file without following symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise IdentityCutoverError("Identity facts must be regular private 0600 files")
            payload = handle.read(_MAX_FACTS_BYTES + 1)
        if not payload or len(payload) > _MAX_FACTS_BYTES:
            raise IdentityCutoverError("Identity facts exceed the bounded input size")
        document = json.loads(payload, object_pairs_hook=_unique_fields)
    except (OSError, ValueError):
        raise IdentityCutoverError("Identity facts could not be read as private JSON documents") from None
    if not isinstance(document, dict):
        raise IdentityCutoverError("Identity facts must be JSON objects")
    return document


def validate_fact_paths(cutover: str | Path | None, bootstrap: str | Path | None) -> None:
    """Reject partial ceremonies and unsafe files before deployment effects."""
    if bool(cutover) != bool(bootstrap):
        raise IdentityCutoverError("Identity cutover and bootstrap fact files must be supplied together")
    if cutover and bootstrap:
        _read_facts(Path(cutover))
        _read_facts(Path(bootstrap))


def _apply(runner: ProcessRunner, document: object) -> None:
    """Apply documents through sensitive stdin, keeping their contents off argv."""
    payload = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    runner.run(["kubectl", "apply", "-f", "-"], input_text=payload, sensitive=True)


def _delete(runner: ProcessRunner, resource: str, *names: str) -> None:
    """Wait for foreground deletion before retiring dependent private bindings."""
    runner.run(
        [
            "kubectl",
            "delete",
            resource,
            *names,
            "-n",
            _NAMESPACE,
            "--ignore-not-found",
            "--cascade=foreground",
            "--wait=true",
            "--timeout=180s",
        ],
        sensitive=True,
    )


def _run_job(runner: ProcessRunner, job: Mapping[str, object], *, timeout: str) -> str:
    """Read a completed bounded Job and remove it after success or failure."""
    name = str(job["metadata"]["name"])
    try:
        _apply(runner, job)
        runner.run(
            ["kubectl", "wait", "--for=condition=complete", f"job/{name}", "-n", _NAMESPACE, f"--timeout={timeout}"],
            sensitive=True,
        )
        return runner.run(["kubectl", "logs", f"job/{name}", "-n", _NAMESPACE, "-c", "portal"], sensitive=True).stdout
    finally:
        _delete(runner, "job", name)


def _probe_state(runner: ProcessRunner, manifest: str) -> tuple[str, int | None]:
    """Require one recognized status marker; return the state and the reported user count."""
    job = render(manifest, job_name=f"identity-cutover-status-{uuid4().hex[:12]}", status_only=True)
    output = _run_job(runner, job, timeout="5m")
    results = [line[len(_RESULT_PREFIX) :] for line in output.splitlines() if line.startswith(_RESULT_PREFIX)]
    try:
        if len(results) != 1:
            raise ValueError
        result = json.loads(results[0], object_pairs_hook=_unique_fields)
        state = result["state"]
        users = result.get("users")
        if state not in _LEGACY_STATES | {"activated"} or not (users is None or isinstance(users, int)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise IdentityCutoverError("Identity status probe was inconclusive; workloads remain unchanged") from None
    return state, users


def _fresh_installation(state: str, users: int | None) -> bool:
    """True for an empty installation, or one whose only user is the prepared bootstrap administrator.

    A legacy database with retained users never qualifies. When generated facts
    meet a different retained plan, the cutover refuses them (#2679).
    """
    if users is None:
        return False
    return (state == "absent" and users == 0) or (state in _LEGACY_STATES - {"absent"} and users <= 1)


def _mount_facts(runner: ProcessRunner, cutover: Path, bootstrap: Path) -> None:
    """Transfer only validated private facts into the exact ceremony Secrets."""
    for name, key, path in zip(_FACT_SECRETS, ("cutover.json", "bootstrap.json"), (cutover, bootstrap), strict=True):
        _apply(
            runner,
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {"name": name, "namespace": _NAMESPACE},
                "stringData": {key: json.dumps(_read_facts(path), separators=(",", ":"))},
            },
        )


@contextmanager
def identity_cutover_boundary(
    runner: ProcessRunner,
    documents: Sequence[dict[str, object]],
    workdir: Path,
    *,
    cutover_facts: str | Path | None = None,
    bootstrap_facts: str | Path | None = None,
    fresh_install_facts: FreshInstallFacts | None = None,
) -> Iterator[CutoverRun]:
    """Keep a failed legacy conversion quiesced and always remove mounted facts."""
    validate_fact_paths(cutover_facts, bootstrap_facts)
    manifest = yaml.safe_dump_all(documents, sort_keys=False)
    # Validate the candidate portal shape before even prerequisite application.
    render(manifest, job_name="identity-cutover-validation", status_only=True)
    _apply(runner, probe_prerequisites(manifest))
    state, users = _probe_state(runner, manifest)
    required = state != "activated"
    run = CutoverRun(required, manifest)
    if required and not cutover_facts and fresh_install_facts is not None and _fresh_installation(state, users):
        # An empty installation needs no reviewed facts: the deploy derives them
        # from the configured operator account (#2679).
        cutover_facts, bootstrap_facts = fresh_install_facts()
        run = CutoverRun(required, manifest, fresh_install=True, bootstrap_facts=Path(bootstrap_facts))
    if not required:
        try:
            yield run
        finally:
            # A prior process may have activated SQL and then lost its cleanup
            # acknowledgment. A verified native retry also retires those facts.
            _delete(runner, "secret", *_FACT_SECRETS)
        return
    if not cutover_facts or not bootstrap_facts:
        raise IdentityCutoverError("Legacy identity conversion requires both private facts files")
    quiesced, _ = quiesce(manifest)
    path = write_private_file(workdir / "identity-quiesced.yaml", quiesced)
    try:
        runner.run(script_argv(get_repo_root(), "enter_identity_maintenance", "--manifest", str(path)))
        _mount_facts(runner, Path(cutover_facts), Path(bootstrap_facts))
        yield run
    finally:
        _delete(runner, "secret", *_FACT_SECRETS)
        path.unlink(missing_ok=True)


def activate_identity_cutover(runner: ProcessRunner, run: CutoverRun) -> None:
    """Only an accepted Job's apply/bootstrap/verify/runtime check permits restore."""
    if run.required:
        _run_job(runner, render(run.manifest, job_name=f"identity-cutover-{uuid4().hex[:12]}"), timeout="30m")


def migrate_policy_datastore(runner: ProcessRunner, documents: Sequence[dict[str, object]]) -> None:
    """Run the exact chart's OpenFGA hook after SQL duties, before activation."""
    jobs = [
        doc
        for doc in documents
        if doc.get("kind") == "Job" and doc.get("metadata", {}).get("name") == "openfga-datastore-migrate"
    ]
    if not jobs:
        return
    if len(jobs) != 1:
        raise IdentityCutoverError("Release contains ambiguous policy migration Jobs")
    prerequisites = [
        doc
        for doc in documents
        if doc.get("kind") != "Job" and "helm.sh/hook" in doc.get("metadata", {}).get("annotations", {})
    ]
    for document in prerequisites:
        _apply(runner, document)
    _delete(runner, "job", "openfga-datastore-migrate")
    _apply(runner, jobs[0])
    runner.run(
        [
            "kubectl",
            "wait",
            "--for=condition=complete",
            "job/openfga-datastore-migrate",
            "-n",
            _NAMESPACE,
            "--timeout=15m",
        ],
        sensitive=True,
    )


def retire_disabled_broker(runner: ProcessRunner, *, enabled: bool) -> None:
    """Remove old compute before additive shared policies can broaden its egress."""
    if enabled:
        return
    names = ("model-broker", "model-access-control", "model-provider-egress")
    _delete(runner, "deployment", *names)
    _delete(runner, "service,poddisruptionbudget", *names)
    _delete(runner, "serviceaccount", "model-broker")
    _delete(runner, "configmap", "model-broker-catalog")
    _delete(
        runner,
        "networkpolicy",
        "model-broker-boundary",
        "model-access-control-ingress",
        "model-provider-egress-boundary",
    )
    runner.run(
        [
            "kubectl",
            "delete",
            "networkpolicy",
            "model-enrollment-control-egress",
            "-n",
            "shifter-jobs",
            "--ignore-not-found",
            "--wait=true",
            "--timeout=180s",
        ],
        sensitive=True,
    )
