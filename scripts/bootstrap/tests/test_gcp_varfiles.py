"""Keep var-file selection and security validation consistent with Terraform."""

import json

import pytest

import bootstrap_core


def layout(root):
    shared = root / "platform/terraform/gcp/environments/platform"
    shared.mkdir(parents=True)
    values = shared.parent / "sample"
    values.mkdir()
    (values / "terraform.tfvars").write_text('public_hostname = "portal.example.test"\nenable_managed_tls = true\n')
    return values


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
