"""Deployment selection must reject retired inputs and isolate shared GCP state."""

import os
import shlex
from pathlib import Path

import pytest

import aws_env_destroy
import bootstrap_core
import cli
import gcp_control_plane
import preflight


@pytest.mark.parametrize("command", ["bootstrap", "terraform", "full", "account-recovery"])
def test_aws_cli_rejects_retired_environment_before_dispatch(command):
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args([command, "--env", "proof", "--profile", "example"])
    assert exc.value.code == 2


def test_aws_teardown_rejects_retired_environment():
    with pytest.raises(SystemExit, match="unsupported teardown environment"):
        aws_env_destroy._validate_env("proof")


def test_bootstrap_config_rejects_retired_environment():
    # Supply a synthetic identity (ADR-069 requires it) so the env check is what fails.
    with pytest.raises(ValueError, match="AWS environment"):
        bootstrap_core.BootstrapConfig(
            env="proof",
            github_org="example-org",
            github_repo="example-repo",
            github_repository_id="123456789",
            github_owner_id="12345678",
        )


@pytest.mark.parametrize("mode", [preflight.Mode.CI, preflight.Mode.LOCAL])
def test_preflight_rejects_retired_environment_before_checking_dependencies(mode):
    def unexpected_tool_check(_name):
        pytest.fail("retired environment reached tool checks")

    report = preflight.run_preflight(preflight.Cloud.AWS, mode, "proof", env={}, tool_exists=unexpected_tool_check)
    assert not report.ok
    assert report.failures[0].name == "environment"


def _gcp_layout(root: Path, environment: str) -> Path:
    shared = root / "platform/terraform/gcp/environments/platform"
    shared.mkdir(parents=True, exist_ok=True)
    values = shared.parent / environment
    values.mkdir()
    (values / "terraform.tfvars").write_text(f'environment = "{environment}"\n')
    (values / "local.auto.tfvars").write_text('public_hostname = "portal.example.test"\n')
    return values


def test_shared_gcp_context_selects_values_and_restores_process_state(tmp_path, monkeypatch):
    values = _gcp_layout(tmp_path, "sample")
    previous_dir = Path.cwd()
    monkeypatch.setenv("TF_DATA_DIR", "previous-data")
    monkeypatch.setenv("TF_CLI_ARGS_apply", "-parallelism=2")
    monkeypatch.delenv("TF_CLI_ARGS_import", raising=False)

    with (
        pytest.raises(RuntimeError, match="simulated failure"),
        gcp_control_plane._gcp_terraform_context(tmp_path, "sample"),
    ):
        assert Path.cwd() == values.parent / "platform"
        assert os.environ["TF_DATA_DIR"] == str(values / ".terraform")
        for operation in ("apply", "plan", "destroy", "import"):
            arguments = shlex.split(os.environ[f"TF_CLI_ARGS_{operation}"])
            assert arguments[-2:] == [
                f"-var-file={values / 'terraform.tfvars'}",
                f"-var-file={values / 'local.auto.tfvars'}",
            ]
        assert shlex.split(os.environ["TF_CLI_ARGS_apply"])[0] == "-parallelism=2"  # noqa: SIM112
        raise RuntimeError("simulated failure")

    assert Path.cwd() == previous_dir
    assert os.environ["TF_DATA_DIR"] == "previous-data"
    assert os.environ["TF_CLI_ARGS_apply"] == "-parallelism=2"  # noqa: SIM112
    assert "TF_CLI_ARGS_import" not in os.environ


def test_shared_gcp_context_does_not_reuse_another_environment(tmp_path):
    first = _gcp_layout(tmp_path, "sample-a")
    second = _gcp_layout(tmp_path, "sample-b")
    for environment, values in (("sample-a", first), ("sample-b", second)):
        with gcp_control_plane._gcp_terraform_context(tmp_path, environment):
            assert os.environ["TF_DATA_DIR"] == str(values / ".terraform")
            assert str(values / "terraform.tfvars") in os.environ["TF_CLI_ARGS_plan"]  # noqa: SIM112
            other_values = second if values == first else first
            assert str(other_values / "terraform.tfvars") not in os.environ["TF_CLI_ARGS_plan"]  # noqa: SIM112


@pytest.mark.parametrize("environment", ["../other", "/outside", "sample\nTF_DATA_DIR=other", "missing", "platform"])
def test_shared_gcp_context_rejects_invalid_selection_before_changing_state(tmp_path, environment):
    _gcp_layout(tmp_path, "sample")
    previous_dir = Path.cwd()
    previous_environment = dict(os.environ)
    with pytest.raises(ValueError), gcp_control_plane._gcp_terraform_context(tmp_path, environment):
        pytest.fail("invalid selection reached Terraform")
    assert Path.cwd() == previous_dir
    assert dict(os.environ) == previous_environment
