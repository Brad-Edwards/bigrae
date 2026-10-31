"""Guest image publication contract of the GCP image workflows (#2668, ADR-037-R9).

``packer-gcp.yml`` builds a guest image, calls ``packer-gcp-validate.yml`` for the
exact candidate, and publishes the validated image to the release registry. A
release carries the published digest, and every deployment imports it. The check
reads both workflows as data and fails closed:

- the build workflow starts only through ``workflow_dispatch`` and selects no
  tenant: it has no ``environment`` or ``publish_target`` input (ADR-037-R9);
- the validation workflow starts only through ``workflow_call``. The build
  workflow calls it from this commit (a local reference) and passes no secrets
  (ADR-004-R23);
- every job that publishes (it authenticates to a cloud, logs in to a registry,
  or pushes with ORAS) binds its fixed Environment (``gcp-image-build``, or
  ``gcp-image-validate`` in the validation workflow), runs on a GitHub-hosted
  runner (ADR-003-R5), and rejects every ref except ``refs/heads/dev`` and
  ``refs/heads/main`` before its first publishing step (ADR-037-R7);
- the job that pushes the guest image needs the validation job, and an
  ``actions/attest-build-provenance`` step in that job signs the pushed digest
  with ``push-to-registry: true`` (ADR-037-R5, R8).
"""

from __future__ import annotations

import re
from pathlib import Path

from .._common import Violation
from .._workflow_model import _dw_is_self_hosted, _dw_load_workflow, _DwShapeError
from ._deploy_workflow_action_pin import _dw_job_steps, _dw_step_uses_cloud_auth
from ._release_workflow_common import (
    CHECK,
    _environment_name,
    _is_true,
    _rejects_refs_outside,
    _step_with,
    _trigger_events,
    _uses_action,
)

GUEST_WORKFLOW_PATH = ".github/workflows/packer-gcp.yml"
VALIDATE_WORKFLOW_PATH = ".github/workflows/packer-gcp-validate.yml"
GUEST_BUILD_ENVIRONMENT = "gcp-image-build"
GUEST_VALIDATE_ENVIRONMENT = "gcp-image-validate"
GUEST_REFS = frozenset({"refs/heads/dev", "refs/heads/main"})
_VALIDATE_USES = f"./{VALIDATE_WORKFLOW_PATH}"
_LOGIN_ACTION = "docker/login-action@"
_ATTEST_ACTION = "actions/attest-build-provenance@"
_ORAS_PUSH = re.compile(r"\boras\s+push\b")
_TENANT_INPUTS = ("environment", "publish_target")


def _violation(rule_id: str, message: str, path: str = GUEST_WORKFLOW_PATH) -> Violation:
    """Return one violation of this check, located in ``path``."""
    return Violation(CHECK, rule_id, path, message)


def _pushes(step: object) -> bool:
    """True when the step runs ``oras push``."""
    return isinstance(step, dict) and bool(_ORAS_PUSH.search(str(step.get("run", ""))))


def _publishes(step: object) -> bool:
    """True when the step authenticates to a cloud, logs in to a registry, or pushes with ORAS."""
    return _dw_step_uses_cloud_auth(step) or _uses_action(step, _LOGIN_ACTION) or _pushes(step)


def _load(repo_root: Path, path: str) -> dict[str, object] | Violation:
    """Parse one workflow, or return the violation that a missing or broken file is."""
    import yaml

    if not (repo_root / path).is_file():
        return _violation("ADR-037-R9", f"{path} is missing", path)
    try:
        return _dw_load_workflow(repo_root, path)
    except (_DwShapeError, yaml.YAMLError) as exc:
        return _violation("ADR-037-R9", f"{path} could not be parsed: {exc}", path)


def _jobs(workflow: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return the jobs of a workflow that are mappings, keyed by job ID."""
    jobs = workflow.get("jobs")
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)} if isinstance(jobs, dict) else {}


def _publishing_job_violations(path: str, job_id: str, job: dict[str, object], environment: str) -> list[Violation]:
    """Check the Environment, the runner, and the ref guard of a job that publishes."""
    steps = _dw_job_steps(job)
    first = next((index for index, step in enumerate(steps) if _publishes(step)), None)
    if first is None:
        return []
    violations: list[Violation] = []
    if _environment_name(job) != environment:
        violations.append(
            _violation(
                "ADR-004-R23", f"job '{job_id}' publishes but does not bind the '{environment}' Environment", path
            )
        )
    if _dw_is_self_hosted(job):
        violations.append(_violation("ADR-003-R5", f"job '{job_id}' publishes on a self-hosted runner", path))
    if not any(_rejects_refs_outside(step, GUEST_REFS) for step in steps[:first]):
        violations.append(
            _violation(
                "ADR-037-R7",
                f"job '{job_id}' has no unconditional step before its first publishing step that rejects every ref "
                "except refs/heads/dev and refs/heads/main",
                path,
            )
        )
    return violations


def _needs(job: dict[str, object]) -> set[str]:
    """Return the job IDs that a job needs."""
    needs = job.get("needs")
    if isinstance(needs, str):
        return {needs}
    return {str(item) for item in needs} if isinstance(needs, list) else set()


def _push_job_violations(job_id: str, job: dict[str, object], validate_jobs: set[str]) -> list[Violation]:
    """Check that a job that pushes a guest needs validation and attests each pushed digest."""
    steps = _dw_job_steps(job)
    push_ids = [str(step.get("id", "")) for step in steps if _pushes(step)]
    violations: list[Violation] = []
    if not validate_jobs or not (_needs(job) & validate_jobs):
        violations.append(
            _violation("ADR-037-R9", f"job '{job_id}' pushes a guest image without needing the validation job")
        )
    for push_id in push_ids:
        digest = f"${{{{ steps.{push_id}.outputs.digest }}}}"
        signed = any(
            _uses_action(step, _ATTEST_ACTION)
            and str(_step_with(step).get("subject-digest", "")).strip() == digest
            and _is_true(_step_with(step).get("push-to-registry"))
            for step in steps
        )
        if not push_id or not signed:
            violations.append(
                _violation(
                    "ADR-037-R5",
                    f"job '{job_id}' pushes a guest image that no attestation step signs by its digest "
                    "with push-to-registry: true",
                )
            )
    return violations


def _trigger_violations(workflow: dict[str, object]) -> list[Violation]:
    """Check that the build starts only from workflow_dispatch and selects no tenant."""
    violations: list[Violation] = []
    on = workflow.get("on")
    if _trigger_events(on) != {"workflow_dispatch"}:
        violations.append(_violation("ADR-037-R9", "packer-gcp.yml must start only from workflow_dispatch"))
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    names = [name for name in _TENANT_INPUTS if isinstance(inputs, dict) and name in inputs]
    violations.extend(
        _violation("ADR-037-R9", f"packer-gcp.yml must select no tenant; remove the '{name}' input") for name in names
    )
    return violations


def _validate_call_violations(job_id: str, job: dict[str, object]) -> list[Violation]:
    """Check that a job that calls validation uses this commit and passes no secrets."""
    uses = str(job.get("uses", "")).strip()
    if not (uses.startswith("./") or "packer-gcp-validate.yml" in uses):
        return []
    violations: list[Violation] = []
    if uses != _VALIDATE_USES:
        violations.append(_violation("ADR-004-R23", f"job '{job_id}' must call {_VALIDATE_USES} from this commit"))
    if "secrets" in job:
        violations.append(_violation("ADR-004-R23", f"job '{job_id}' must pass no secrets to validation"))
    return violations


def _build_workflow_violations(workflow: dict[str, object]) -> list[Violation]:
    """Every violation of the build workflow: triggers, validation calls, publication, and pushes."""
    violations = _trigger_violations(workflow)
    jobs = _jobs(workflow)
    validate_jobs = {job_id for job_id, job in jobs.items() if str(job.get("uses", "")).strip() == _VALIDATE_USES}
    for job_id, job in jobs.items():
        violations.extend(_validate_call_violations(job_id, job))
        violations.extend(_publishing_job_violations(GUEST_WORKFLOW_PATH, job_id, job, GUEST_BUILD_ENVIRONMENT))
        if any(_pushes(step) for step in _dw_job_steps(job)):
            violations.extend(_push_job_violations(job_id, job, validate_jobs))
    return violations


def _validate_workflow_violations(workflow: dict[str, object]) -> list[Violation]:
    """Every violation of the validation workflow: its trigger and its publishing jobs."""
    violations: list[Violation] = []
    if _trigger_events(workflow.get("on")) != {"workflow_call"}:
        violations.append(
            _violation(
                "ADR-004-R23", "packer-gcp-validate.yml must start only from workflow_call", VALIDATE_WORKFLOW_PATH
            )
        )
    for job_id, job in _jobs(workflow).items():
        violations.extend(_publishing_job_violations(VALIDATE_WORKFLOW_PATH, job_id, job, GUEST_VALIDATE_ENVIRONMENT))
    return violations


def _guest_publication_violations(repo_root: Path) -> list[Violation]:
    """Every violation of the guest image publication contract."""
    violations: list[Violation] = []
    build = _load(repo_root, GUEST_WORKFLOW_PATH)
    violations.extend([build] if isinstance(build, Violation) else _build_workflow_violations(build))
    validate = _load(repo_root, VALIDATE_WORKFLOW_PATH)
    violations.extend([validate] if isinstance(validate, Violation) else _validate_workflow_violations(validate))
    return violations
