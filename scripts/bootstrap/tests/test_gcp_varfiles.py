"""Keep var-file selection and security validation consistent with Terraform."""

import json
import os
import shlex
from pathlib import Path

import pytest

import bootstrap_core
import gcp_control_plane


def layout(root):
    shared = root / "platform/terraform/gcp/environments/platform"
    shared.mkdir(parents=True)
    values = shared.parent / "sample"
    values.mkdir()
    (values / "terraform.tfvars").write_text('public_hostname = "portal.example.test"\nenable_managed_tls = true\n')
    return values


def test_json_varfiles_keep_terraform_precedence(tmp_path):
    values = layout(tmp_path)
    (values / "terraform.tfvars.json").write_text("{}")
    (values / "10-settings.auto.tfvars.json").write_text("{}")
    (values / "20-settings.auto.tfvars").write_text('region = "us-central1"\n')
    with gcp_control_plane._gcp_terraform_context(tmp_path, "sample"):
        flags = shlex.split(os.environ["TF_CLI_ARGS_apply"])  # noqa: SIM112 - Terraform uses a lowercase command suffix.
        names = [Path(flag.split("=", 1)[1]).name for flag in flags if flag.startswith("-var-file=")]
        assert names == [
            "terraform.tfvars",
            "terraform.tfvars.json",
            "10-settings.auto.tfvars.json",
            "20-settings.auto.tfvars",
        ]


def test_json_tls_override_is_checked_before_apply(tmp_path):
    values = layout(tmp_path)
    (values / "local.auto.tfvars.json").write_text(json.dumps({"enable_managed_tls": False}))
    with pytest.raises(ValueError, match="managed TLS"):
        bootstrap_core.validate_gcp_control_plane_security_inputs(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_hostname", {"private-test-marker": "value"}),
        ("enable_managed_tls", "private-test-marker"),
        ("gke_master_authorized_cidrs", "private-test-marker"),
        ("gke_master_authorized_cidrs", ["10.0.0.0/8", {"private-test-marker": "value"}]),
    ],
)
def test_invalid_json_security_shapes_fail_without_exposing_values(tmp_path, field, value):
    values = layout(tmp_path)
    (values / "local.auto.tfvars.json").write_text(json.dumps({field: value}))
    with pytest.raises(ValueError, match=field) as exc:
        bootstrap_core.validate_gcp_control_plane_security_inputs(values)
    assert "private-test-marker" not in str(exc.value)


@pytest.mark.parametrize("contents", ["{", "[]"])
def test_invalid_json_document_fails_before_apply(tmp_path, contents):
    values = layout(tmp_path)
    (values / "local.auto.tfvars.json").write_text(contents)
    with pytest.raises(ValueError, match="GCP Terraform JSON variables"):
        bootstrap_core.validate_gcp_control_plane_security_inputs(values)


def test_security_validation_uses_the_same_mixed_file_order_as_terraform(tmp_path):
    values = layout(tmp_path)
    (values / "terraform.tfvars.json").write_text(json.dumps({"enable_managed_tls": False}))
    (values / "10-settings.auto.tfvars").write_text("enable_managed_tls = true\n")
    (values / "20-settings.auto.tfvars.json").write_text(json.dumps({"public_hostname": "selected.example.test"}))
    settings = bootstrap_core.read_gcp_control_plane_security_inputs(values)
    assert settings["enable_managed_tls"] is True
    assert settings["public_hostname"] == "selected.example.test"
    bootstrap_core.validate_gcp_control_plane_security_inputs(values)


def test_json_only_environment_values_are_selected_for_every_operation(tmp_path):
    values = layout(tmp_path)
    (values / "terraform.tfvars").unlink()
    json_vars = values / "terraform.tfvars.json"
    json_vars.write_text(json.dumps({"environment": "sample"}))
    with gcp_control_plane._gcp_terraform_context(tmp_path, "sample"):
        for operation in ("plan", "apply", "destroy", "import"):
            assert shlex.split(os.environ[f"TF_CLI_ARGS_{operation}"])[-1] == f"-var-file={json_vars}"
