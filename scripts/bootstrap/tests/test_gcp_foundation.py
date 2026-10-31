"""Fresh foundation ordering and fail-closed target binding."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from bootstrap_core import get_repo_root, set_assume_yes
from gcp_foundation import DESTROY_PROTECTED_REFS, bootstrap_gcp_foundation, expected_trust


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
                "github_repo": "bigrae",
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


def _plan_document(inputs_path, *, extra_binding=False):
    """Return a saved-plan document that grants exactly the trust that the inputs authorize."""
    expected = expected_trust(json.loads(Path(inputs_path).read_text()))
    principal = (
        f"principal://iam.googleapis.com/projects/{expected['project_number']}/locations/global/"
        f"workloadIdentityPools/{expected['name_prefix']}-github/subject/"
    )
    accounts = expected["service_accounts"]
    build = accounts.get("build")
    resources = [
        (
            "google_service_account_iam_member",
            {"service_account_id": build, "role": role, "member": "serviceAccount:" + build.rsplit("/", 1)[-1]},
        )
        for role in ("roles/iam.serviceAccountUser", "roles/iam.serviceAccountTokenCreator")
        if build
    ]
    if any(expected["purpose_subjects"].values()):
        resources.append(
            (
                "google_iam_workload_identity_pool_provider",
                {
                    "project": expected["project_id"],
                    "workload_identity_pool_id": expected["name_prefix"] + "-github",
                    "workload_identity_pool_provider_id": "github",
                    "attribute_condition": expected["attribute_condition"],
                    "attribute_mapping": {"google.subject": "assertion.sub"},
                    "oidc": [{"issuer_uri": "https://token.actions.githubusercontent.com", "allowed_audiences": []}],
                },
            )
        )
    for purpose, subjects in expected["purpose_subjects"].items():
        resources += [
            (
                "google_service_account_iam_member",
                {
                    "service_account_id": accounts[purpose],
                    "role": "roles/iam.workloadIdentityUser",
                    "member": principal + subject,
                },
            )
            for subject in subjects
        ]
    for purpose, members in expected["impersonation"].items():
        resources += [
            (
                "google_service_account_iam_member",
                {
                    "service_account_id": accounts[purpose],
                    "role": "roles/iam.serviceAccountTokenCreator",
                    "member": member,
                },
            )
            for member in members
        ]
    if extra_binding:
        resources.append(
            (
                "google_service_account_iam_member",
                {
                    "service_account_id": build,
                    "role": "roles/iam.workloadIdentityUser",
                    "member": principal + "repo:another/repo:environment:x",
                },
            )
        )
    return json.dumps(
        {
            "resource_changes": [
                {
                    "address": f"module.cicd_oidc_identity.{kind}.r{index}",
                    "type": kind,
                    "change": {"actions": ["create"], "after": after, "after_unknown": {}},
                }
                for index, (kind, after) in enumerate(resources)
            ]
        }
    )


class FoundationProcess:
    """Simulate gcloud, terraform, and gh for one foundation bootstrap."""

    def __init__(self, inputs_path, *, outputs=None, extra_binding=False, bucket_exists=False):
        self.inputs_path = inputs_path
        self.outputs = outputs or {}
        self.extra_binding = extra_binding
        self.bucket_exists = bucket_exists
        self.calls = []
        self.stdin = {}
        self.envs = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(dict(kwargs.get("env") or os.environ))
        if kwargs.get("input") is not None:
            self.stdin[tuple(argv)] = kwargs["input"]
        if argv[:3] == ["gcloud", "projects", "describe"]:
            return subprocess.CompletedProcess(argv, 0, "123\n")
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repos/") and "/environments/" not in argv[2]:
            return subprocess.CompletedProcess(argv, 0, '{"id":456,"owner":{"id":789}}')
        if argv[:4] == ["gcloud", "storage", "buckets", "describe"]:
            return subprocess.CompletedProcess(argv, 0 if self.bucket_exists else 1, "")
        if argv[0] == "terraform" and argv[2:4] == ["show", "-json"]:
            return subprocess.CompletedProcess(
                argv, 0, _plan_document(self.inputs_path, extra_binding=self.extra_binding)
            )
        if argv[0] == "terraform" and argv[2:4] == ["output", "-json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({k: {"value": v} for k, v in self.outputs.items()}))
        return subprocess.CompletedProcess(argv, 0, "")

    def terraform(self, verb):
        return [i for i, call in enumerate(self.calls) if call[0] == "terraform" and call[2] == verb]


def test_new_foundation_creates_backend_and_checks_the_saved_plan_before_apply(inputs, monkeypatch):
    process = FoundationProcess(inputs)
    monkeypatch.setenv("GOOGLE_CLOUD_QUOTA_PROJECT", "stale-project")
    monkeypatch.setattr(subprocess, "run", process)
    set_assume_yes(True)
    try:
        bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    calls = process.calls
    create = next(i for i, c in enumerate(calls) if c[:4] == ["gcloud", "storage", "buckets", "create"])
    (init,), (plan_index,), (show,), (apply,) = (process.terraform(verb) for verb in ("init", "plan", "show", "apply"))
    assert create < init < plan_index < show < apply
    assert calls[plan_index][2:5] == ["plan", "-input=false", f"-var-file={inputs}"]
    plan = calls[plan_index][-1].removeprefix("-out=")
    assert plan.startswith(tempfile.gettempdir())
    assert calls[show][2:] == ["show", "-json", plan]
    assert calls[apply][2:] == ["apply", "-input=false", plan]
    # A destroy-only foundation writes no image Environment.
    assert not any(call[:2] == ["gh", "secret"] for call in calls)
    # Every child uses the foundation project as its quota project (#2584).
    assert {env.get("GOOGLE_CLOUD_QUOTA_PROJECT") for env in process.envs} == {"example-project"}
    assert os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] == "stale-project"
    # Terraform keeps its backend configuration in a private data directory (#2586).
    data_dirs = {
        env.get("TF_DATA_DIR") for env, call in zip(process.envs, calls, strict=True) if call[0] == "terraform"
    }
    (data_dir,) = data_dirs
    assert data_dir.startswith(tempfile.gettempdir()) and data_dir == str(Path(plan).parent / "terraform-data")


def test_a_saved_plan_with_unauthorized_trust_stops_before_apply(inputs, monkeypatch):
    process = FoundationProcess(inputs, extra_binding=True)
    monkeypatch.setattr(subprocess, "run", process)
    set_assume_yes(True)
    try:
        with pytest.raises(ValueError, match="differs from the authorized trust"):
            bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    assert process.terraform("show") and not process.terraform("apply")


def _builder_inputs(path):
    example = json.loads((get_repo_root() / "scripts/bootstrap/gcp-image-builder.example.tfvars.json").read_text())
    path.write_text(
        json.dumps({**example, "project_number": "123", "github_repository_id": "456", "github_owner_id": "789"})
    )
    return path


_BUILDER_OUTPUTS = {
    "workload_identity_provider": (
        "projects/123/locations/global/workloadIdentityPools/shifter-images-github/providers/github"
    ),
    "packer_build_service_account_email": "shifterimages-packer@example-builds.iam.gserviceaccount.com",
    "packer_validate_service_account_email": "shifterimages-validate@example-builds.iam.gserviceaccount.com",
    "image_build_network_name": "shifter-images-image-build",
    "image_build_subnetwork_name": "shifter-images-image-build",
    "image_export_bucket_name": "example-builds-shifter-images-image-export",
}


def test_builder_foundation_writes_the_image_environments_with_secrets_on_stdin(tmp_path, monkeypatch):
    inputs = _builder_inputs(tmp_path / "builder.json")
    process = FoundationProcess(inputs, outputs=_BUILDER_OUTPUTS)
    monkeypatch.setattr(subprocess, "run", process)
    set_assume_yes(True)
    try:
        bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    calls = process.calls
    apply = process.terraform("apply")[0]
    environment_calls = [i for i, call in enumerate(calls) if call[0] == "gh" and "/environments/" in " ".join(call)]
    assert environment_calls and min(environment_calls) > apply
    for environment in ("gcp-image-build", "gcp-image-validate"):
        api = f"repos/example-org/example-repo/environments/{environment}"
        assert [
            "gh",
            "api",
            "--method",
            "PUT",
            api,
            "-F",
            "deployment_branch_policy[protected_branches]=false",
            "-F",
            "deployment_branch_policy[custom_branch_policies]=true",
        ] in calls
        branches = [
            call[-3].removeprefix("name=")
            for call in calls
            if call[:5] == ["gh", "api", "--method", "POST", f"{api}/deployment-branch-policies"]
        ]
        assert branches == ["dev", "main"]
    secrets = {
        (call[3], call[5]): value for call, value in ((list(argv), value) for argv, value in process.stdin.items())
    }
    assert (
        secrets[("GCP_PACKER_BUILD_SERVICE_ACCOUNT", "gcp-image-build")]
        == _BUILDER_OUTPUTS["packer_build_service_account_email"]
    )
    assert (
        secrets[("GCP_PACKER_VALIDATE_SERVICE_ACCOUNT", "gcp-image-validate")]
        == _BUILDER_OUTPUTS["packer_validate_service_account_email"]
    )
    assert secrets[("GCP_PROJECT_ID", "gcp-image-build")] == "example-builds"
    # No identity secret reaches the argv of a gh command.
    gh_tokens = [token for call in calls if call[0] == "gh" for token in call]
    for name in (
        "workload_identity_provider",
        "packer_build_service_account_email",
        "packer_validate_service_account_email",
    ):
        assert not any(_BUILDER_OUTPUTS[name] in token for token in gh_tokens)
    variables = {(call[3], call[5]): call[-1] for call in calls if call[:3] == ["gh", "variable", "set"]}
    assert variables[("GCP_GCE_BASE_IMAGE_BUCKET", "gcp-image-build")] == _BUILDER_OUTPUTS["image_export_bucket_name"]
    assert variables[("GCP_PACKER_SUBNETWORK", "gcp-image-validate")] == _BUILDER_OUTPUTS["image_build_subnetwork_name"]
    assert ("GCP_GCE_BASE_IMAGE_BUCKET", "gcp-image-validate") not in variables


def test_builder_foundation_without_image_outputs_fails_before_writing_secrets(tmp_path, monkeypatch):
    inputs = _builder_inputs(tmp_path / "builder.json")
    outputs = {**_BUILDER_OUTPUTS, "image_export_bucket_name": ""}
    process = FoundationProcess(inputs, outputs=outputs)
    monkeypatch.setattr(subprocess, "run", process)
    set_assume_yes(True)
    try:
        with pytest.raises(ValueError, match="enable_image_export_storage"):
            bootstrap_gcp_foundation(str(inputs))
    finally:
        set_assume_yes(False)
    assert not any(call[:2] == ["gh", "secret"] for call in process.calls)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda data: data.update({"release_evidence_bucket_name": "example-builds-evidence"}),
            "release_evidence_bucket_name must be",
        ),
        (lambda data: data["purpose_contexts"]["build"][0].update({"ref": "refs/heads/feature"}), "must bind"),
        (
            lambda data: data["purpose_contexts"]["validate"][0].update({"environment": "gcp-validate-dev"}),
            "'gcp-image-validate' GitHub Environment",
        ),
        (
            lambda data: data["purpose_contexts"]["validate"][0].update(
                {"workflow_ref": "example-org/example-repo/.github/workflows/packer-gcp-validate.yml@refs/heads/dev"}
            ),
            "must name the workflow",
        ),
    ],
)
def test_builder_inputs_fail_before_commands(change, message, tmp_path, monkeypatch):
    inputs = _builder_inputs(tmp_path / "builder.json")
    data = json.loads(inputs.read_text())
    change(data)
    inputs.write_text(json.dumps(data))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("ran a command"))
    with pytest.raises(ValueError, match=message):
        bootstrap_gcp_foundation(str(inputs))


def test_duplicate_input_keys_fail_before_commands(tmp_path, monkeypatch):
    path = tmp_path / "foundation.json"
    path.write_text('{"project_id": "a", "project_id": "b"}')
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("ran a command"))
    with pytest.raises(ValueError, match="repeat the key"):
        bootstrap_gcp_foundation(str(path))


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


def test_destroy_refs_match_the_terraform_contract():
    root = get_repo_root()
    allowed = "contains([" + ", ".join(f'"{ref}"' for ref in DESTROY_PROTECTED_REFS) + "], context.ref)"
    for inventory in (
        "platform/terraform/gcp/global/cicd-oidc/inventory.tf",
        "platform/terraform/gcp/modules/cicd-oidc-identity/inventory.tf",
    ):
        assert allowed in (root / inventory).read_text()


def _github_less_inputs(path, **overrides):
    example_path = get_repo_root() / "scripts/bootstrap/gcp-foundation.github-less.example.tfvars.json"
    example = json.loads(example_path.read_text())
    path.write_text(json.dumps({**example, "project_number": "123", **overrides}))
    return path


def test_github_less_foundation_uses_no_github_and_disables_federation(tmp_path, monkeypatch):
    """ADR-070: no gh call, no repository IDs, and explicit empty federation inputs."""
    inputs = _github_less_inputs(tmp_path / "foundation.json")
    federation = {}
    process = FoundationProcess(inputs, bucket_exists=True)

    def run(argv, **kwargs):
        if argv[0] == "terraform" and argv[2] == "plan":
            override = next(a for a in argv if a.startswith("-var-file=") and a.endswith("github-less.tfvars.json"))
            with open(override.removeprefix("-var-file="), encoding="utf-8") as handle:
                federation.update(json.load(handle))
        return process(argv, **kwargs)

    calls = process.calls
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
