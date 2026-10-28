"""Fresh foundation ordering and fail-closed target binding."""

import json
import subprocess
import tempfile

import pytest

from bootstrap_core import get_repo_root, set_assume_yes
from gcp_foundation import DESTROY_PROTECTED_REFS, bootstrap_gcp_foundation


def _destroy(ref, workflow_ref=None):
    return {
        "destroy": [
            {
                "environment": "example-destroy",
                "ref": ref,
                "workflow_ref": workflow_ref or f"example/shifter/.github/workflows/gcp-dev-destroy.yml@{ref}",
                "reusable_workflow_ref": "",
            }
        ]
    }


def _write_inputs(path, purpose_contexts):
    path.write_text(
        json.dumps(
            {
                "project_id": "example-project",
                "project_number": "123",
                "environment": "example",
                "name_prefix": "shifter-example",
                "github_org": "example",
                "github_repo": "shifter",
                "github_repository_id": "456",
                "github_owner_id": "789",
                "purpose_contexts": purpose_contexts,
                "terraform_state_bucket_name": "example-project-state",
                "release_evidence_bucket_name": "example-project-evidence",
            }
        )
    )
    return path


@pytest.fixture
def inputs(tmp_path):
    return _write_inputs(tmp_path / "foundation.json", _destroy("refs/heads/dev"))


def test_new_foundation_creates_backend_before_saved_plan_apply(inputs, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gcloud", "projects", "describe"]:
            return subprocess.CompletedProcess(argv, 0, "123\n")
        if argv[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(argv, 0, '{"id":456,"owner":{"id":789}}')
        if argv[:4] == ["gcloud", "storage", "buckets", "describe"]:
            return subprocess.CompletedProcess(argv, 1, "")
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(subprocess, "run", run)
    set_assume_yes(True)
    try:
        bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    create = next(i for i, c in enumerate(calls) if c[:4] == ["gcloud", "storage", "buckets", "create"])
    init = next(i for i, c in enumerate(calls) if c[0] == "terraform" and c[2] == "init")
    assert create < init
    assert calls[-2][2:5] == ["plan", "-input=false", f"-var-file={inputs}"]
    plan = calls[-2][-1].removeprefix("-out=")
    assert plan.startswith(tempfile.gettempdir())
    assert calls[-1][2:] == ["apply", "-input=false", plan]


def test_wrong_project_number_stops_before_mutation(inputs, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "999\n")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="project number"):
        bootstrap_gcp_foundation(str(inputs))
    assert len(calls) == 1


def test_incomplete_inputs_fail_before_commands(tmp_path, monkeypatch):
    path = tmp_path / "incomplete.json"
    path.write_text("{}")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="Missing foundation inputs"):
        bootstrap_gcp_foundation(str(path))
    assert calls == []


@pytest.mark.parametrize("ref", DESTROY_PROTECTED_REFS)
def test_dry_run_executes_no_commands(ref, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    bootstrap_gcp_foundation(str(_write_inputs(tmp_path / "foundation.json", _destroy(ref))), dry_run=True)
    assert calls == []


@pytest.mark.parametrize(
    "purpose_contexts",
    [
        _destroy("refs/heads/balrog"),
        _destroy("refs/heads/dev", "example/shifter/.github/workflows/gcp-dev-destroy.yml@refs/heads/balrog"),
    ],
)
def test_unprotected_destroy_tuple_fails_before_commands(purpose_contexts, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="Destroy purpose tuple"):
        bootstrap_gcp_foundation(str(_write_inputs(tmp_path / "foundation.json", purpose_contexts)))
    assert calls == []


def test_destroy_refs_match_workflow_guard_and_terraform_contract():
    root = get_repo_root()
    workflow = (root / ".github/workflows/gcp-dev-destroy.yml").read_text()
    assert f"{'|'.join(DESTROY_PROTECTED_REFS)}) ;;" in workflow
    allowed = "contains([" + ", ".join(f'"{ref}"' for ref in DESTROY_PROTECTED_REFS) + "], context.ref)"
    for inventory in (
        "platform/terraform/gcp/global/cicd-oidc/inventory.tf",
        "platform/terraform/gcp/modules/cicd-oidc-identity/inventory.tf",
    ):
        assert allowed in (root / inventory).read_text()
    example = json.loads((root / "scripts/bootstrap/gcp-foundation.example.tfvars.json").read_text())
    assert all(context["ref"] in DESTROY_PROTECTED_REFS for context in example["purpose_contexts"]["destroy"])


def _github_less_inputs(path, **overrides):
    example_path = get_repo_root() / "scripts/bootstrap/gcp-foundation.github-less.example.tfvars.json"
    example = json.loads(example_path.read_text())
    path.write_text(json.dumps({**example, "project_number": "123", **overrides}))
    return path


def test_github_less_foundation_uses_no_github_and_disables_federation(tmp_path, monkeypatch):
    """ADR-070: no gh call, no repository IDs, and explicit empty federation inputs."""
    inputs = _github_less_inputs(tmp_path / "foundation.json")
    calls = []
    federation = {}

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gcloud", "projects", "describe"]:
            return subprocess.CompletedProcess(argv, 0, "123\n")
        if argv[:4] == ["gcloud", "storage", "buckets", "describe"]:
            return subprocess.CompletedProcess(argv, 0, "")
        if argv[0] == "terraform" and argv[2] == "plan":
            override = next(a for a in argv if a.startswith("-var-file=") and a.endswith("github-less.tfvars.json"))
            with open(override.removeprefix("-var-file="), encoding="utf-8") as handle:
                federation.update(json.load(handle))
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(subprocess, "run", run)
    set_assume_yes(True)
    try:
        bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    assert not any(call[0] == "gh" for call in calls)
    assert federation == {"purpose_contexts": {}, "github_repository_id": "", "github_owner_id": ""}
    plan = next(call for call in calls if call[0] == "terraform" and call[2] == "plan")
    assert plan[4] == f"-var-file={inputs}"
    assert not any(call[:4] == ["gcloud", "storage", "buckets", "create"] for call in calls)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"github_repository_id": "456"}, "takes no GitHub inputs; remove github_repository_id"),
        ({"github_org": "example"}, "takes no GitHub inputs; remove github_org"),
        ({"impersonation_principals": {"deploy": ["user:a@example.com"]}}, "release_scan"),
        (
            {"impersonation_principals": {"deploy": [], "release_scan": ["user:a@x.com"], "destroy": ["user:a@x.com"]}},
            "deploy",
        ),
        (
            {"deployment_config_bucket_name": "--billing-project=other"},
            "deployment_config_bucket_name is not a valid GCP name",
        ),
        (
            {"terraform_state_bucket_name": "example-project-other-state"},
            "terraform_state_bucket_name must be 'example-project-terraform-state'",
        ),
        (
            {"release_evidence_bucket_name": "example-project-evidence"},
            "release_evidence_bucket_name must be 'example-project-release-evidence'",
        ),
    ],
)
def test_github_less_inputs_fail_before_commands(overrides, message, tmp_path, monkeypatch):
    inputs = _github_less_inputs(tmp_path / "foundation.json", **overrides)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match=message):
        bootstrap_gcp_foundation(str(inputs))
    assert calls == []


def test_github_less_requires_the_config_bucket(tmp_path, monkeypatch):
    inputs = _github_less_inputs(tmp_path / "foundation.json")
    data = json.loads(inputs.read_text())
    data.pop("deployment_config_bucket_name")
    inputs.write_text(json.dumps(data))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("ran a command"))
    with pytest.raises(ValueError, match="Missing foundation inputs: deployment_config_bucket_name"):
        bootstrap_gcp_foundation(str(inputs))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", "--impersonate-service-account=attacker@example.iam.gserviceaccount.com"),
        ("project_id", "Example-Project"),
        ("terraform_state_bucket_name", "--billing-project=other"),
        ("terraform_state_bucket_name", "example/state"),
        ("region", "--flags-file=/tmp/flags.yaml"),
        ("region", 7),
    ],
)
def test_invalid_gcp_name_fails_before_commands(field, value, tmp_path, monkeypatch):
    path = _write_inputs(tmp_path / "foundation.json", _destroy("refs/heads/dev"))
    path.write_text(json.dumps({**json.loads(path.read_text()), field: value}))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match=f"Foundation input {field} is not a valid GCP name"):
        bootstrap_gcp_foundation(str(path))
    assert calls == []
