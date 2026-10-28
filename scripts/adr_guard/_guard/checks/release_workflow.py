"""Provenance contract of the GitHub-less GCP release workflow (#2571, ADR-070).

``.github/workflows/release-gcp.yml`` builds the release that a GitHub-less GCP
deployment consumes: the five control-plane images, their signed provenance, the
release manifest, and the deployer image. A deployment trusts a release only
through the provenance that this workflow signs, so the workflow must keep these
properties. The check reads the workflow as data and fails closed:

- only ``workflow_dispatch`` starts the workflow (ADR-004-R23);
- every job that authenticates to GCP binds the ``gcp-release`` Environment
  (ADR-004-R23) and runs on a GitHub-hosted runner (ADR-003-R5);
- in that job, a step before the authentication step rejects every ref except
  ``refs/heads/main`` (ADR-037-R7);
- every non-local action is pinned to a full commit SHA (ADR-037-R1);
- every pushed image build produces an SBOM and maximum provenance, and an
  ``actions/attest-build-provenance`` step in the same job signs the exact
  digest with ``push-to-registry: true`` (ADR-037-R5). The registry copy of the
  Sigstore bundle is what a deployment verifies without GitHub;
- the job that builds the deployer image scans that exact digest with the
  blocking Trivy gate before it attests the image (ADR-070-R1). The only
  accepted exposures are the time-boxed entries in the deployer ignore file.
"""

from __future__ import annotations

import re
from pathlib import Path

from .._common import Violation, is_guard_source_path
from .._workflow_model import _dw_is_self_hosted, _dw_load_workflow, _DwShapeError
from ._deploy_workflow_action_pin import (
    _dw_iter_uses_refs,
    _dw_job_steps,
    _dw_step_uses,
    _dw_step_uses_cloud_auth,
    _dw_uses_is_sha_pinned,
)

RELEASE_WORKFLOW_PATH = ".github/workflows/release-gcp.yml"
RELEASE_ENVIRONMENT = "gcp-release"
_CHECK = "release-workflow-provenance"
_BUILD_ACTION = "docker/build-push-action@"
_ATTEST_ACTION = "actions/attest-build-provenance@"
# The ref guard is a shell `case` on GITHUB_REF whose only admitted branch is
# refs/heads/main and whose fallback branch exits non-zero.
_MAIN_ONLY_CASE = re.compile(
    r'case\s+"\$\{GITHUB_REF\}"\s+in\s+refs/heads/main\)\s*;;\s*\*\)(?P<fallback>.*?);;\s*esac',
    re.DOTALL,
)
_STEP_DIGEST_REF = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.digest\s*\}\}")
DEPLOYER_DOCKERFILE = "platform/deployer/gcp/Dockerfile"
DEPLOYER_IGNORE_FILE = "platform/deployer/gcp/trivyignore.yaml"
# Every token of the blocking gate. A step that drops one of them is not the gate.
_DEPLOYER_SCAN_TOKENS = (
    "trivy",
    " image ",
    "--severity HIGH,CRITICAL",
    "--ignore-unfixed",
    "--exit-code 1",
    f"--ignorefile {DEPLOYER_IGNORE_FILE}",
)


def _violation(rule_id: str, message: str) -> Violation:
    """Build one violation of this check against the release workflow."""
    return Violation(_CHECK, rule_id, RELEASE_WORKFLOW_PATH, message)


def _relevant(files: list[str] | None) -> bool:
    """True when a changed file can affect the release workflow contract."""
    if files is None:
        return True
    return any(path == RELEASE_WORKFLOW_PATH or is_guard_source_path(path) for path in files)


def _is_true(value: object) -> bool:
    """True for a YAML boolean true or the string ``"true"``."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _trigger_events(on: object) -> set[str]:
    """Return the event names of a workflow ``on:`` value."""
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(event) for event in on}
    if isinstance(on, dict):
        return {str(event) for event in on}
    return set()


def _environment_name(job: dict[str, object]) -> str:
    """Return the job's GitHub Environment name, or ``""`` when it binds none."""
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return environment.strip() if isinstance(environment, str) else ""


def _rejects_non_main_refs(step: object) -> bool:
    """True when the step always runs and fails on every ref except refs/heads/main."""
    if not isinstance(step, dict) or "if" in step or _is_true(step.get("continue-on-error")):
        return False
    script = step.get("run")
    if not isinstance(script, str):
        return False
    match = _MAIN_ONLY_CASE.search(script)
    return bool(match and re.search(r"\bexit\s+1\b", match.group("fallback")))


def _step_with(step: object) -> dict[str, object]:
    """Return a step's ``with:`` mapping, or an empty mapping."""
    if isinstance(step, dict) and isinstance(step.get("with"), dict):
        return step["with"]
    return {}


def _auth_job_violations(job_id: str, job: dict[str, object], steps: list[object]) -> list[Violation]:
    """Environment, runner, and ref-guard rules for a job that authenticates to GCP."""
    violations: list[Violation] = []
    first_auth = next(index for index, step in enumerate(steps) if _dw_step_uses_cloud_auth(step))
    if _environment_name(job) != RELEASE_ENVIRONMENT:
        violations.append(
            _violation(
                "ADR-004-R23",
                f"job '{job_id}' authenticates to GCP but does not bind the '{RELEASE_ENVIRONMENT}' Environment",
            )
        )
    if _dw_is_self_hosted(job):
        violations.append(
            _violation("ADR-003-R5", f"job '{job_id}' builds the release on a self-hosted runner; use GitHub-hosted")
        )
    if not any(_rejects_non_main_refs(step) for step in steps[:first_auth]):
        violations.append(
            _violation(
                "ADR-037-R7",
                f"job '{job_id}' has no unconditional step before GCP authentication that rejects every ref "
                "except refs/heads/main",
            )
        )
    return violations


def _attestation_violations(job_id: str, steps: list[object]) -> list[Violation]:
    """SBOM, provenance, and registry-attestation rules for the image builds of one job."""
    violations: list[Violation] = []
    attested: set[str] = set()
    for step in steps:
        uses = _dw_step_uses(step) or ""
        if not uses.startswith(_ATTEST_ACTION):
            continue
        settings = _step_with(step)
        if not _is_true(settings.get("push-to-registry")):
            violations.append(
                _violation(
                    "ADR-037-R5",
                    f"job '{job_id}' has an attestation step without push-to-registry: true; a deployment "
                    "verifies the registry copy of the bundle",
                )
            )
        attested.update(_STEP_DIGEST_REF.findall(str(settings.get("subject-digest", ""))))
    for step in steps:
        uses = _dw_step_uses(step) or ""
        settings = _step_with(step)
        if not uses.startswith(_BUILD_ACTION) or not _is_true(settings.get("push")):
            continue
        step_id = step.get("id") if isinstance(step, dict) else None
        label = f"build step '{step_id}'" if step_id else "an unnamed build step"
        if str(settings.get("provenance", "")).strip() != "mode=max" or not _is_true(settings.get("sbom")):
            violations.append(
                _violation("ADR-037-R5", f"job '{job_id}' {label} must set provenance: mode=max and sbom: true")
            )
        if not step_id or step_id not in attested:
            violations.append(
                _violation(
                    "ADR-037-R5",
                    f"job '{job_id}' {label} pushes an image that no attestation step signs by its exact digest",
                )
            )
    return violations


def _compact_script(step: dict[str, object]) -> str:
    """Return a step's run script on one line, with shell line continuations removed."""
    return " ".join(str(step.get("run", "")).replace("\\\n", " ").split())


def _scans_deployer_digest(step: object, build_id: str) -> bool:
    """True when the step always runs the blocking Trivy gate on the deployer build's digest."""
    if not isinstance(step, dict) or "if" in step or _is_true(step.get("continue-on-error")):
        return False
    script = f" {_compact_script(step)} "
    digest = f"steps.{build_id}.outputs.digest"
    return all(token in script for token in _DEPLOYER_SCAN_TOKENS) and digest in str(step.get("env", "")) + script


def _deployer_scan_violations(job_id: str, steps: list[object]) -> list[Violation]:
    """The deployer image is scanned by its exact digest before its attestation."""
    builds = [
        (index, step)
        for index, step in enumerate(steps)
        if (_dw_step_uses(step) or "").startswith(_BUILD_ACTION)
        and DEPLOYER_DOCKERFILE in str(_step_with(step).get("file", ""))
    ]
    violations: list[Violation] = []
    for index, step in builds:
        build_id = step.get("id") if isinstance(step, dict) else None
        attest_index = next(
            (
                later
                for later in range(index + 1, len(steps))
                if (_dw_step_uses(steps[later]) or "").startswith(_ATTEST_ACTION)
                and build_id in _STEP_DIGEST_REF.findall(str(_step_with(steps[later]).get("subject-digest", "")))
            ),
            len(steps),
        )
        if not build_id or not any(_scans_deployer_digest(scan, build_id) for scan in steps[index + 1 : attest_index]):
            violations.append(
                _violation(
                    "ADR-070-R1",
                    f"job '{job_id}' must scan the deployer image digest with the blocking Trivy gate "
                    f"(--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --ignorefile {DEPLOYER_IGNORE_FILE}) "
                    "before it attests the image",
                )
            )
    return violations


def _workflow_violations(workflow: dict[str, object]) -> list[Violation]:
    """Every contract violation of one parsed release workflow."""
    violations: list[Violation] = []
    events = _trigger_events(workflow.get("on"))
    if events != {"workflow_dispatch"}:
        violations.append(
            _violation(
                "ADR-004-R23",
                f"the release workflow must start only from workflow_dispatch, not {sorted(events) or 'no event'}",
            )
        )
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return [*violations, _violation("ADR-037-R5", "the release workflow has no jobs")]
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = _dw_job_steps(job)
        if any(_dw_step_uses_cloud_auth(step) for step in steps):
            violations.extend(_auth_job_violations(job_id, job, steps))
        violations.extend(_attestation_violations(job_id, steps))
        violations.extend(_deployer_scan_violations(job_id, steps))
    for job_id, ref in _dw_iter_uses_refs(workflow):
        if not ref.startswith("./") and not _dw_uses_is_sha_pinned(ref):
            violations.append(_violation("ADR-037-R1", f"job '{job_id}' uses '{ref}'; pin a full 40-hex commit SHA"))
    return violations


def check_release_workflow(repo_root: Path, files: list[str] | None) -> list[Violation]:
    """Fail closed unless the GCP release workflow keeps its provenance contract."""
    # Local import: keeps PyYAML optional for non-workflow checks.
    import yaml

    if not _relevant(files):
        return []
    if not (repo_root / RELEASE_WORKFLOW_PATH).is_file():
        return [_violation("ADR-037-R5", "the GCP release workflow is missing")]
    try:
        workflow = _dw_load_workflow(repo_root, RELEASE_WORKFLOW_PATH)
    except (_DwShapeError, yaml.YAMLError) as exc:
        return [_violation("ADR-037-R5", f"the release workflow could not be parsed: {exc}")]
    return _workflow_violations(workflow)
