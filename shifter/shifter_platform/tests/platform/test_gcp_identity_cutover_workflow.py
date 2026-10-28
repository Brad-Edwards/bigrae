"""The GCP release performs one fail-closed S8 maintenance cut."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github/workflows/_gcp-dev.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_identity_cutover_stops_effects_before_conversion_and_restores_after_verify() -> None:
    workflow = _workflow()
    prerequisites = workflow.index("name: Install identity probe prerequisites")
    probe = workflow.index("name: Probe the one-time identity cutover state")
    quiesce = workflow.index("name: Enter the identity maintenance boundary")
    migrate = workflow.index("name: Run database migrations")
    policy = workflow.index("name: Reconcile the private policy datastore")
    cutover = workflow.index("name: Apply and verify the identity hard cut")
    restore = workflow.index("name: Restart control-plane deployments")
    assert prerequisites < probe < quiesce < migrate < policy < cutover < restore
    prerequisite_step = workflow[prerequisites:probe]
    assert "--probe-prerequisites" in prerequisite_step
    assert 'kubectl apply -f "${PLATFORM_QUIESCED_MANIFEST}"' not in prerequisite_step
    maintenance = workflow[quiesce:migrate]
    assert 'enter_identity_maintenance.py --manifest "${PLATFORM_QUIESCED_MANIFEST}"' in maintenance
    assert maintenance.index("enter_identity_maintenance.py") < maintenance.index("kubectl create secret")
    assert "< <(" not in maintenance and "2>/dev/null || true" not in maintenance
    assert "if: env.IDENTITY_CUTOVER_REQUIRED == 'true'" in maintenance
    cutover_step = workflow[cutover:restore]
    assert "render_identity_cutover_job.py" in cutover_step
    assert "identity-cutover-facts" in cutover_step
    assert "exit 1" in cutover_step
    assert 'kubectl apply -f "${PLATFORM_NATIVE_MANIFEST}"' not in cutover_step
    assert 'kubectl apply -f "${PLATFORM_NATIVE_MANIFEST}"' in workflow[restore:]


def test_cutover_facts_are_file_mounted_not_forwarded_as_job_environment() -> None:
    workflow = _workflow()
    assert "--from-file=cutover.json=" in workflow
    assert "--from-file=bootstrap.json=" in workflow
    assert "--from-literal" not in workflow
    assert "IDENTITY_CUTOVER_FACTS_JSON" not in workflow[workflow.index("name: Apply and verify") :]


@pytest.mark.parametrize(
    ("state", "wait_status", "expected"),
    [
        ("activated", 0, "false"),
        ("absent", 0, "true"),
        ("planned", 0, "true"),
        ("sql_applied", 0, "true"),
        ("policy_pending", 0, "true"),
        ("activated", 1, None),
        ("unknown", 0, None),
        ("invalid-json", 0, None),
    ],
)
def test_probe_requires_confirmed_status_before_requesting_maintenance(tmp_path, state, wait_status, expected):
    steps = next(
        job["steps"]
        for job in yaml.safe_load(_workflow())["jobs"].values()
        if any(step.get("name") == "Probe the one-time identity cutover state" for step in job.get("steps", []))
    )
    script = next(step["run"] for step in steps if step.get("name") == "Probe the one-time identity cutover state")
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, body in {
        "uv": "exit 0",
        "kubectl": 'case "$1" in wait) exit "$PROBE_WAIT_STATUS";; logs) printf \'%s\\n\' "$PROBE_RESULT";; esac',
    }.items():
        executable = binary / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o700)
    output = tmp_path / "output"
    output.touch()
    # Execute only the repository-owned workflow with synthetic command adapters.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_ENV": str(output),
            "PLATFORM_NATIVE_MANIFEST": "synthetic.yaml",
            "PROBE_WAIT_STATUS": str(wait_status),
            "PROBE_RESULT": "Waiting for database...\nSkipping migrations\nRunning: status\n"
            + "SHIFTER_IDENTITY_CUTOVER_RESULT="
            + ("not-json" if state == "invalid-json" else f'{{"state":"{state}"}}')
            + "\n",
        },
    )
    if expected is None:
        assert result.returncode != 0
        assert "IDENTITY_CUTOVER_REQUIRED=" not in output.read_text()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"IDENTITY_CUTOVER_REQUIRED={expected}\n"
