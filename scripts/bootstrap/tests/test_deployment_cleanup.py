"""Deployment selection must reject retired inputs and isolate shared GCP state."""

import pytest

import aws_env_destroy
import bootstrap_core
import cli
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

    report = preflight.run_preflight(
        preflight.Cloud.AWS, mode, "proof", host=preflight.PreflightHost(env={}, tool_exists=unexpected_tool_check)
    )
    assert not report.ok
    assert report.failures[0].name == "environment"
