"""Caller contract of the GCP release workflow (#2660, ADR-004-R23).

Only one job of ``release-please.yml`` may call ``release-gcp.yml``. That
workflow starts only on a push to ``main``. The calling job needs the Release
Please job and runs only when a release tag points at the commit. That fact, the
version, and the commit (``source_revision``) come from the unconditional
``release-commit`` step after the Release Please action. The job inherits no
secrets.
"""

from __future__ import annotations

from pathlib import Path

from .._common import Violation
from .._workflow_model import _dw_job_if, _dw_load_workflow, _DwShapeError
from .._workflow_model_expr import _dw_normalize_expr
from ._deploy_workflow_action_pin import _dw_iter_workflow_files, _dw_job_steps
from ._release_workflow_common import (
    RELEASE_PLEASE_WORKFLOW_PATH,
    RELEASE_WORKFLOW_PATH,
    _is_true,
    _step_with,
    _uses_action,
    _violation,
)

_CALL_USES = f"./{RELEASE_WORKFLOW_PATH}"
_RELEASE_PLEASE_ACTION = "googleapis/release-please-action@"
# The step of the Release Please job that finds the release tag of the commit,
# and the outputs it gives to gate the call and bind its inputs.
_RELEASE_COMMIT_STEP = "release-commit"
_RELEASE_OUTPUTS = ("release_commit", "version", "sha")
# Caller input name and the Release Please output that it must pass.
_CALL_INPUTS = (("version", "version"), ("source_revision", "sha"))


def _release_callers(repo_root: Path) -> list[tuple[str, str, dict[str, object]]]:
    """Return ``(path, job_id, job)`` for each workflow job that calls the release workflow.

    A workflow that does not parse cannot run, so it cannot call the release workflow.
    """
    # Local import: keeps PyYAML optional for non-workflow checks.
    import yaml

    callers: list[tuple[str, str, dict[str, object]]] = []
    for path in _dw_iter_workflow_files(repo_root):
        try:
            jobs = _dw_load_workflow(repo_root, path).get("jobs")
        except (_DwShapeError, yaml.YAMLError):
            continue
        if not isinstance(jobs, dict):
            continue
        callers.extend(
            (path, str(job_id), job)
            for job_id, job in jobs.items()
            if isinstance(job, dict) and str(job.get("uses", "")).strip().startswith(_CALL_USES)
        )
    return callers


def _release_please_job(jobs: dict[str, object]) -> tuple[str, dict[str, object], int] | None:
    """Return ``(job_id, job, step_index)`` for the step that runs the Release Please action, or None."""
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for index, step in enumerate(_dw_job_steps(job)):
            if _uses_action(step, _RELEASE_PLEASE_ACTION):
                return str(job_id), job, index
    return None


def _is_release_commit_step(step: object) -> bool:
    """True for the unconditional run step that finds the release tag of the commit."""
    return (
        isinstance(step, dict)
        and step.get("id") == _RELEASE_COMMIT_STEP
        and isinstance(step.get("run"), str)
        and "if" not in step
        and not _is_true(step.get("continue-on-error"))
    )


def _release_output_violations(rp_job: str, job: dict[str, object], action_index: int) -> list[Violation]:
    """The Release Please job takes the release outputs from its release-commit step after the action."""
    violations: list[Violation] = []
    if not any(_is_release_commit_step(step) for step in _dw_job_steps(job)[action_index + 1 :]):
        violations.append(
            _violation(
                "ADR-004-R23",
                f"job '{rp_job}' must run an unconditional '{_RELEASE_COMMIT_STEP}' step "
                "after the Release Please action",
                RELEASE_PLEASE_WORKFLOW_PATH,
            )
        )
    outputs = job.get("outputs") if isinstance(job.get("outputs"), dict) else {}
    expected = {name: f"${{{{ steps.{_RELEASE_COMMIT_STEP}.outputs.{name} }}}}" for name in _RELEASE_OUTPUTS}
    violations.extend(
        _violation("ADR-004-R23", f"job '{rp_job}' must set output {name}: {value}", RELEASE_PLEASE_WORKFLOW_PATH)
        for name, value in expected.items()
        if _dw_normalize_expr(outputs.get(name)) != value
    )
    return violations


def _push_to_main_only(on: object) -> bool:
    """True when the workflow starts only on a push to ``main``."""
    if not isinstance(on, dict) or set(on) != {"push"} or not isinstance(on["push"], dict):
        return False
    return set(on["push"]) == {"branches"} and on["push"]["branches"] == ["main"]


def _needs(job: dict[str, object]) -> set[str]:
    """Return the job IDs that a job needs."""
    needs = job.get("needs")
    if isinstance(needs, str):
        return {needs}
    return {str(item) for item in needs} if isinstance(needs, list) else set()


def _caller_job_violations(job_id: str, job: dict[str, object], rp_job: str) -> list[Violation]:
    """Gate, input, and secret rules for the job of release-please.yml that calls the release workflow."""
    problems: list[str] = []
    if str(job.get("uses", "")).strip() != _CALL_USES:
        problems.append(f"call {_CALL_USES} from this commit, with no ref")
    if rp_job not in _needs(job) or _dw_job_if(job) != f"needs.{rp_job}.outputs.release_commit == 'true'":
        problems.append(f"need job '{rp_job}' and run only when if: needs.{rp_job}.outputs.release_commit == 'true'")
    for name, output in _CALL_INPUTS:
        expected = f"${{{{ needs.{rp_job}.outputs.{output} }}}}"
        if _dw_normalize_expr(_step_with(job).get(name)) != expected:
            problems.append(f"pass {name}: {expected}")
    if "secrets" in job:
        problems.append("inherit or pass no secrets")
    return [
        _violation(
            "ADR-004-R23", f"job '{job_id}' calls the release workflow and must {problem}", RELEASE_PLEASE_WORKFLOW_PATH
        )
        for problem in problems
    ]


def _caller_violations(repo_root: Path) -> list[Violation]:
    """Only a gated release-please.yml job calls the release workflow, after a release on main."""
    callers = _release_callers(repo_root)
    violations = [
        _violation(
            "ADR-004-R23", f"job '{job_id}' calls the release workflow; only release-please.yml may call it", path
        )
        for path, job_id, _ in callers
        if path != RELEASE_PLEASE_WORKFLOW_PATH
    ]
    own = [(job_id, job) for path, job_id, job in callers if path == RELEASE_PLEASE_WORKFLOW_PATH]
    if len(own) != 1:
        return [
            *violations,
            _violation(
                "ADR-004-R23",
                f"exactly one job must call the release workflow for a release commit, not {len(own)}",
                RELEASE_PLEASE_WORKFLOW_PATH,
            ),
        ]
    workflow = _dw_load_workflow(repo_root, RELEASE_PLEASE_WORKFLOW_PATH)
    if not _push_to_main_only(workflow.get("on")):
        violations.append(
            _violation(
                "ADR-004-R23", "release-please.yml must start only on a push to main", RELEASE_PLEASE_WORKFLOW_PATH
            )
        )
    jobs = workflow.get("jobs") if isinstance(workflow.get("jobs"), dict) else {}
    release_please = _release_please_job(jobs)
    if release_please is None:
        return [
            *violations,
            _violation(
                "ADR-004-R23", "release-please.yml has no Release Please action step", RELEASE_PLEASE_WORKFLOW_PATH
            ),
        ]
    rp_job, rp_definition, action_index = release_please
    return [
        *violations,
        *_release_output_violations(rp_job, rp_definition, action_index),
        *_caller_job_violations(*own[0], rp_job),
    ]
