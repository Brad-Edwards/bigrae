"""The CI adapter renders the GitHub-less deployment input bundle (#2571, ADR-070)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import render_ci_deployment_inputs as adapter

ROOT_CONFIG = {
    "version": 1,
    "backend": "gcp",
    "deployment": {"name": "shifter", "profile": "dev"},
    "secrets": {"django_secret_key": "prompt"},
    "settings": {"dynamic_secret_project_id": "example-secrets"},
}


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "SHIFTER_CONFIG_GCP_DEV": yaml.safe_dump(ROOT_CONFIG),
        "GCP_PROJECT_ID": "example-project",
        "GCP_PUBLIC_HOSTNAME": "portal.example.test",
        "GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN": "example.test",
        "GCP_REGION": "us-east1",
        "GCP_RANGE_BACKEND": "gce",
        "RANGE_NETWORK_ZONE": "us-east1-b",
        "RANGE_NETWORK_ZONES": "us-east1-b, us-central1-a",
        "GCP_RANGE_CELL_PROJECT_ID": "example-ranges",
        "GCP_RANGE_HOST_IDENTITY_POOL_SIZE": "2",
        "GCP_RANGE_PRIVATE_GOOGLE_ACCESS": "true",
        "GCP_RANGE_IMAGE_KEY_PROFILES_JSON": '{"kali": {"key": "value"}}',
        "GCP_RANGE_LINUX_IMAGE": "projects/example-project/global/images/shifter-ubuntu-1",
        "GCP_RANGE_DC_IMAGE": "projects/example-project/global/images/shifter-dc-1",
        "RANGE_OPENVPN_ENABLED": "true",
        "GCP_CTF_CONTENT_BUCKET": "example-ctf",
        "GCP_BOOTSTRAP_ADMIN_EMAIL": "ops@example.test",
        "GCP_BOOTSTRAP_ADMIN_PASSWORD": "never-written",
        "PLATFORM_BOOTSTRAP_STAFF_EMAILS": "staff@example.test",
        "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS": "root@example.test",
        "SMOKE_TEST_USER_EMAIL": "smoke@example.test",
        "GCP_FOUNDATION_INPUTS_JSON": '{"project_id": "example-project"}',
    }
    env.update(overrides)
    return env


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    values = root / "platform/terraform/gcp/environments/acme/terraform.tfvars"
    values.parent.mkdir(parents=True)
    values.write_text('public_hostname = "shifter.example.com"\nenable_managed_tls = true\n')
    return root


def _render(repo: Path, tmp_path: Path, env: dict[str, str]) -> Path:
    output = tmp_path / "bundle"
    adapter.render_bundle("acme", output, env, repo_root=repo)
    return output


def test_bundle_projects_every_github_input_onto_its_home(repo: Path, tmp_path: Path):
    output = _render(repo, tmp_path, _env())
    config = yaml.safe_load((output / "bigrae.yaml").read_text())
    settings = config["settings"]
    assert config["deployment"]["domain"] == "portal.example.test"
    assert (settings["project_id"], settings["region"], settings["ctf_content_bucket"]) == (
        "example-project",
        "us-east1",
        "example-ctf",
    )
    assert settings["range_cells"] == {
        "backend": "gce",
        "zone": "us-east1-b",
        "zones": ["us-east1-b", "us-central1-a"],
        "project_id": "example-ranges",
        "host_identity_pool_size": 2,
        "private_google_access": True,
        "image_key_profiles": {"kali": {"key": "value"}},
    }
    assert settings["participant_openvpn"] == {"enabled": True}
    assert settings["operator_bootstrap"] == {
        "admin_email": "ops@example.test",
        "staff_emails": ["staff@example.test"],
        "superuser_emails": ["root@example.test"],
    }
    assert settings["post_deploy_smoke"] == {"user_email": "smoke@example.test"}
    assert json.loads((output / "terraform.tfvars.json").read_text()) == {
        "gke_master_authorized_cidrs": [],
        "identity_allowed_email_domain": "example.test",
    }
    assert (output / "terraform.tfvars").read_text().startswith("public_hostname")
    assert json.loads((output / "range-images.json").read_text()) == {
        "dc": "projects/example-project/global/images/shifter-dc-1",
        "linux": "projects/example-project/global/images/shifter-ubuntu-1",
    }
    assert json.loads((output / "foundation-inputs.json").read_text()) == {"project_id": "example-project"}
    assert output.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert not any("never-written" in path.read_text() for path in output.iterdir())


def test_committed_broker_template_wins_over_the_overlay_variable(repo: Path, tmp_path: Path):
    deploy = repo / "platform/deploy/gcp/acme"
    deploy.mkdir(parents=True)
    (deploy / "model-broker-overlay.template.json").write_text('{"settings": {}}')
    (deploy / "retired-cluster-artifacts.json").write_text("{}")
    overlay = json.dumps({"settings": {"model_access": {"enabled": False}}})
    output = _render(repo, tmp_path, _env(SHIFTER_CONFIG_OVERLAY_JSON=overlay))
    assert (output / "model-broker-overlay.template.json").is_file()
    assert (output / "retired-cluster-artifacts.json").is_file()
    assert "model_access" not in yaml.safe_load((output / "bigrae.yaml").read_text())["settings"]


def test_overlay_variable_applies_without_a_template(repo: Path, tmp_path: Path):
    overlay = json.dumps({"settings": {"model_access": {"enabled": False}}})
    output = _render(repo, tmp_path, _env(SHIFTER_CONFIG_OVERLAY_JSON=overlay))
    assert yaml.safe_load((output / "bigrae.yaml").read_text())["settings"]["model_access"] == {"enabled": False}


def test_destroy_inputs_need_no_operator(repo: Path, tmp_path: Path):
    """The destroy Environment holds no operator secrets; a teardown seeds no operator."""
    env = _env(GCP_BOOTSTRAP_ADMIN_EMAIL="", PLATFORM_BOOTSTRAP_STAFF_EMAILS="", PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS="")
    output = tmp_path / "bundle"
    adapter.render_bundle("acme", output, env, purpose="destroy", repo_root=repo)
    assert yaml.safe_load((output / "bigrae.yaml").read_text())["settings"]["operator_bootstrap"] == {"enabled": False}
    with pytest.raises(adapter.CiInputsError, match="--purpose"):
        adapter.render_bundle("acme", tmp_path / "other", env, purpose="apply", repo_root=repo)


def test_operator_opt_out_needs_no_operator_email(repo: Path, tmp_path: Path):
    env = _env(SHIFTER_SKIP_OPERATOR_BOOTSTRAP="true", GCP_BOOTSTRAP_ADMIN_EMAIL="")
    settings = yaml.safe_load((_render(repo, tmp_path, env) / "bigrae.yaml").read_text())["settings"]
    assert settings["operator_bootstrap"]["enabled"] is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"GCP_PROJECT_ID": ""}, "GCP_PROJECT_ID is not set"),
        ({"GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN": ""}, "GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN is not set"),
        ({"RANGE_OPENVPN_ENABLED": "yes"}, "RANGE_OPENVPN_ENABLED must be true or false"),
        ({"GCP_RANGE_HOST_IDENTITY_POOL_SIZE": "-1"}, "non-negative integer"),
        ({"GCP_RANGE_IMAGE_KEY_PROFILES_JSON": "[1]"}, "must be a JSON object"),
        ({"SHIFTER_CONFIG_GCP_DEV": "- not a mapping"}, "root config with a settings mapping"),
        (
            {"SHIFTER_CONFIG_GCP_DEV": yaml.safe_dump({**ROOT_CONFIG, "settings": {"project_id": "other-project"}})},
            "GCP_PROJECT_ID conflicts with settings.project_id",
        ),
        ({"GCP_BOOTSTRAP_ADMIN_EMAIL": ""}, "settings.operator_bootstrap"),
        ({"SHIFTER_CONFIG_OVERLAY_JSON": '{"settings": {"secrets": {}}}'}, "SHIFTER_CONFIG_OVERLAY_JSON is invalid"),
    ],
)
def test_invalid_inputs_fail_and_leave_no_bundle(repo: Path, tmp_path: Path, overrides, message):
    output = tmp_path / "bundle"
    with pytest.raises(adapter.CiInputsError, match=message):
        adapter.render_bundle("acme", output, _env(**overrides), repo_root=repo)
    assert not output.exists()


def test_unknown_deployment_and_existing_output_fail(repo: Path, tmp_path: Path):
    with pytest.raises(adapter.CiInputsError, match="no committed Terraform values"):
        adapter.render_bundle("other", tmp_path / "x", _env(), repo_root=repo)
    with pytest.raises(adapter.CiInputsError, match="lowercase DNS label"):
        adapter.render_bundle("Bad_Name", tmp_path / "x", _env(), repo_root=repo)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("keep")
    with pytest.raises(FileExistsError):
        adapter.render_bundle("acme", existing, _env(), repo_root=repo)
    assert (existing / "keep").read_text() == "keep"


def test_main_reports_errors_without_values(repo: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(adapter, "REPO_ROOT", repo)
    for key, value in _env(GCP_PROJECT_ID="").items():
        monkeypatch.setenv(key, value)
    assert adapter.main(["--environment", "acme", "--output", str(tmp_path / "bundle")]) == 1
    captured = capsys.readouterr()
    assert "GCP_PROJECT_ID is not set" in captured.err
    assert "never-written" not in captured.err + captured.out
    monkeypatch.setenv("GCP_PROJECT_ID", "example-project")
    assert adapter.main(["--environment", "acme", "--output", str(tmp_path / "bundle")]) == 0
    assert "never-written" not in capsys.readouterr().out
