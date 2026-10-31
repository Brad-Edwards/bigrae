"""Provenance contract of the GitHub-less GCP release workflow (#2571, ADR-070).

``.github/workflows/release-gcp.yml`` builds the release that a GitHub-less GCP
deployment consumes: the five control-plane images, their signed provenance, the
release manifest, and the deployer image. A deployment trusts a release only
through the provenance that this workflow signs, so the workflow must keep these
properties. The check reads the workflow as data and fails closed:

- only ``workflow_dispatch`` and ``workflow_call`` start the workflow
  (ADR-004-R23, #2660). The ``workflow_call`` trigger requires the
  ``source_revision`` input;
- the only caller is one job of ``release-please.yml``. That workflow starts
  only on a push to ``main``. The calling job needs the Release Please job and
  runs only when a release tag points at the commit. That fact, the version,
  and the commit (``source_revision``) come from the unconditional
  ``release-commit`` step after the Release Please action. The job inherits no
  secrets (ADR-004-R23, #2660);
- every job that publishes (it logs in to a registry, authenticates to a cloud,
  or pushes an image) binds the ``gcp-release`` Environment (ADR-004-R23) and
  runs on a GitHub-hosted runner (ADR-003-R5);
- in that job, a step before the first publishing step rejects every ref except
  ``refs/heads/main``, ``refs/heads/dev``, and ``refs/heads/deploy-test``
  (ADR-037-R7). The release registry is public (#2659), so the registry login
  is the credential that the guard protects;
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
    _dw_step_uses_cloud_auth,
    _dw_uses_is_sha_pinned,
)
from ._release_workflow_caller import _caller_violations
from ._release_workflow_common import (
    RELEASE_WORKFLOW_PATH,
    _environment_name,
    _is_true,
    _rejects_refs_outside,
    _step_with,
    _trigger_events,
    _uses_action,
    _violation,
)
from ._release_workflow_guests import _guest_publication_violations

_WORKFLOWS_PREFIX = ".github/workflows/"
RELEASE_TRIGGERS = frozenset({"workflow_dispatch", "workflow_call"})
RELEASE_ENVIRONMENT = "gcp-release"
_BUILD_ACTION = "docker/build-push-action@"
_ATTEST_ACTION = "actions/attest-build-provenance@"
_LOGIN_ACTION = "docker/login-action@"
# The ref guard admits only the release refs.
RELEASE_REFS = frozenset({"refs/heads/main", "refs/heads/dev", "refs/heads/deploy-test"})
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


def _relevant(files: list[str] | None) -> bool:
    """True when a changed file can affect the release workflow contract.

    Any workflow file can add a caller of the release workflow.
    """
    if files is None:
        return True
    return any(path.startswith(_WORKFLOWS_PREFIX) or is_guard_source_path(path) for path in files)


def _rejects_non_release_refs(step: object) -> bool:
    """True when the step always runs and fails on every ref outside ``RELEASE_REFS``."""
    return _rejects_refs_outside(step, RELEASE_REFS)


def _subject_build_ids(step: object) -> list[str]:
    """Return the IDs of the build steps whose digest output the step signs."""
    return _STEP_DIGEST_REF.findall(str(_step_with(step).get("subject-digest", "")))


def _publishes(step: object) -> bool:
    """True when the step logs in to a registry, authenticates to a cloud, or pushes an image."""
    return (
        _dw_step_uses_cloud_auth(step)
        or _uses_action(step, _LOGIN_ACTION)
        or (_uses_action(step, _BUILD_ACTION) and _is_true(_step_with(step).get("push")))
    )


def _publish_job_violations(job_id: str, job: dict[str, object], steps: list[object]) -> list[Violation]:
    """Environment, runner, and ref-guard rules for a job that publishes the release."""
    violations: list[Violation] = []
    first_publish = next(index for index, step in enumerate(steps) if _publishes(step))
    if _environment_name(job) != RELEASE_ENVIRONMENT:
        violations.append(
            _violation(
                "ADR-004-R23",
                f"job '{job_id}' publishes the release but does not bind the '{RELEASE_ENVIRONMENT}' Environment",
            )
        )
    if _dw_is_self_hosted(job):
        violations.append(
            _violation("ADR-003-R5", f"job '{job_id}' builds the release on a self-hosted runner; use GitHub-hosted")
        )
    if not any(_rejects_non_release_refs(step) for step in steps[:first_publish]):
        violations.append(
            _violation(
                "ADR-037-R7",
                f"job '{job_id}' has no unconditional step before its first publishing step that rejects every ref "
                "except refs/heads/main, refs/heads/dev, and refs/heads/deploy-test",
            )
        )
    return violations


def _build_step_violations(job_id: str, step: object, attested: set[str]) -> list[Violation]:
    """SBOM, provenance, and attestation rules for one pushed image build."""
    settings = _step_with(step)
    step_id = step.get("id") if isinstance(step, dict) else None
    label = f"build step '{step_id}'" if step_id else "an unnamed build step"
    violations: list[Violation] = []
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


def _attestation_violations(job_id: str, steps: list[object]) -> list[Violation]:
    """SBOM, provenance, and registry-attestation rules for the image builds of one job."""
    attestations = [step for step in steps if _uses_action(step, _ATTEST_ACTION)]
    violations = [
        _violation(
            "ADR-037-R5",
            f"job '{job_id}' has an attestation step without push-to-registry: true; a deployment "
            "verifies the registry copy of the bundle",
        )
        for step in attestations
        if not _is_true(_step_with(step).get("push-to-registry"))
    ]
    attested = {build_id for step in attestations for build_id in _subject_build_ids(step)}
    for step in steps:
        if _uses_action(step, _BUILD_ACTION) and _is_true(_step_with(step).get("push")):
            violations.extend(_build_step_violations(job_id, step, attested))
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


def _is_deployer_build(step: object) -> bool:
    """True when the step builds the deployer image."""
    return _uses_action(step, _BUILD_ACTION) and DEPLOYER_DOCKERFILE in str(_step_with(step).get("file", ""))


def _attestation_index(steps: list[object], start: int, build_id: object) -> int:
    """Return the index of the first step from ``start`` that attests ``build_id``, or ``len(steps)``."""
    return next(
        (
            index
            for index in range(start, len(steps))
            if _uses_action(steps[index], _ATTEST_ACTION) and build_id in _subject_build_ids(steps[index])
        ),
        len(steps),
    )


def _deployer_scan_violations(job_id: str, steps: list[object]) -> list[Violation]:
    """The deployer image is scanned by its exact digest before its attestation."""
    violations: list[Violation] = []
    for index, step in enumerate(steps):
        if not _is_deployer_build(step):
            continue
        build_id = step.get("id") if isinstance(step, dict) else None
        scans = steps[index + 1 : _attestation_index(steps, index + 1, build_id)]
        if not build_id or not any(_scans_deployer_digest(scan, build_id) for scan in scans):
            violations.append(
                _violation(
                    "ADR-070-R1",
                    f"job '{job_id}' must scan the deployer image digest with the blocking Trivy gate "
                    f"(--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --ignorefile {DEPLOYER_IGNORE_FILE}) "
                    "before it attests the image",
                )
            )
    return violations


def _call_requires_source_revision(on: object) -> bool:
    """True when the ``workflow_call`` trigger declares a required ``source_revision`` input."""
    call = on.get("workflow_call") if isinstance(on, dict) else None
    inputs = call.get("inputs") if isinstance(call, dict) else None
    revision = inputs.get("source_revision") if isinstance(inputs, dict) else None
    return isinstance(revision, dict) and _is_true(revision.get("required"))


def _trigger_violations(workflow: dict[str, object]) -> list[Violation]:
    """Only workflow_dispatch and workflow_call start the release workflow."""
    on = workflow.get("on")
    events = _trigger_events(on)
    if events != RELEASE_TRIGGERS:
        return [
            _violation(
                "ADR-004-R23",
                "the release workflow must start only from workflow_dispatch and workflow_call, "
                f"not {sorted(events) or 'no event'}",
            )
        ]
    if not _call_requires_source_revision(on):
        return [_violation("ADR-004-R23", "the workflow_call trigger must require the source_revision input")]
    return []


def _job_violations(job_id: str, job: dict[str, object]) -> list[Violation]:
    """Every contract violation of one release workflow job."""
    steps = _dw_job_steps(job)
    violations: list[Violation] = []
    if any(_publishes(step) for step in steps):
        violations.extend(_publish_job_violations(job_id, job, steps))
    violations.extend(_attestation_violations(job_id, steps))
    violations.extend(_deployer_scan_violations(job_id, steps))
    return violations


def _pin_violations(workflow: dict[str, object]) -> list[Violation]:
    """Every non-local action is pinned to a full commit SHA."""
    return [
        _violation("ADR-037-R1", f"job '{job_id}' uses '{ref}'; pin a full 40-hex commit SHA")
        for job_id, ref in _dw_iter_uses_refs(workflow)
        if not ref.startswith("./") and not _dw_uses_is_sha_pinned(ref)
    ]


def _workflow_violations(workflow: dict[str, object]) -> list[Violation]:
    """Every contract violation of one parsed release workflow."""
    violations = _trigger_violations(workflow)
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return [*violations, _violation("ADR-037-R5", "the release workflow has no jobs")]
    for job_id, job in jobs.items():
        if isinstance(job, dict):
            violations.extend(_job_violations(job_id, job))
    return [*violations, *_pin_violations(workflow)]


def _load_release_workflow(repo_root: Path) -> dict[str, object] | Violation:
    """Return the parsed release workflow, or the violation that stops the check."""
    # Local import: keeps PyYAML optional for non-workflow checks.
    import yaml

    if not (repo_root / RELEASE_WORKFLOW_PATH).is_file():
        return _violation("ADR-037-R5", "the GCP release workflow is missing")
    try:
        return _dw_load_workflow(repo_root, RELEASE_WORKFLOW_PATH)
    except (_DwShapeError, yaml.YAMLError) as exc:
        return _violation("ADR-037-R5", f"the release workflow could not be parsed: {exc}")


def check_release_workflow(repo_root: Path, files: list[str] | None) -> list[Violation]:
    """Fail closed unless the GCP release workflow keeps its provenance contract."""
    if not _relevant(files):
        return []
    workflow = _load_release_workflow(repo_root)
    if isinstance(workflow, Violation):
        return [workflow, *_guest_publication_violations(repo_root)]
    return [*_workflow_violations(workflow), *_caller_violations(repo_root), *_guest_publication_violations(repo_root)]
