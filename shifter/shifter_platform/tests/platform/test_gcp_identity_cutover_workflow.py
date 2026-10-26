"""The GCP release performs one fail-closed S8 maintenance cut."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github/workflows/_gcp-dev.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_identity_cutover_stops_effects_before_conversion_and_restores_after_verify() -> None:
    workflow = _workflow()
    prerequisites = workflow.index("name: Install identity probe prerequisites with workloads stopped")
    probe = workflow.index("name: Probe the one-time identity cutover state")
    quiesce = workflow.index("name: Enter the identity maintenance boundary")
    migrate = workflow.index("name: Run database migrations")
    policy = workflow.index("name: Reconcile the private policy datastore")
    cutover = workflow.index("name: Apply and verify the identity hard cut")
    restore = workflow.index("name: Restart control-plane deployments")
    assert prerequisites < probe < quiesce < migrate < policy < cutover < restore
    prerequisite_step = workflow[prerequisites:probe]
    assert 'kubectl apply -f "${PLATFORM_QUIESCED_MANIFEST}"' in prerequisite_step
    maintenance = workflow[quiesce:migrate]
    assert "kubectl delete horizontalpodautoscaler" in maintenance
    assert '"spec":{"suspend":true}' in maintenance
    assert "kubectl scale" in maintenance
    assert "status.active" in maintenance and "condition=complete" in maintenance
    assert 'kubectl apply -f "${PLATFORM_QUIESCED_MANIFEST}"' not in maintenance
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
