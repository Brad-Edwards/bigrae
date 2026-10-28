"""Tests for the generated Helm values of a GCP deployment (#2571, #27, #1816, #2535)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gcp_inputs import load_deployment_inputs
from tests.gcp_test_support import (
    PROJECT,
    FakeRunner,
    generated_helm_values,
    root_config,
    sample_gcp_outputs,
    write_bundle,
)


def _values(tmp_path: Path, outputs=None, **settings: object) -> dict[str, object]:
    inputs = load_deployment_inputs(
        str(write_bundle(tmp_path / "bundle", config=root_config(**settings))), tmp_path / "work", FakeRunner(), {}
    )
    return generated_helm_values(inputs, sample_gcp_outputs(PROJECT) if outputs is None else outputs)


def test_build_values_rejects_missing_broker_readback(tmp_path: Path):
    """A deploy must not silently run disabled from stale Terraform output."""
    broker = {
        "enabled": True,
        "hostname": "models.example.test",
        "vip": "10.40.0.25",
        "admitted_subnets": ["10.50.1.0/24"],
        "tls_secret_name": "broker-tls-v1",
        "control_tls_secret_name": "control-tls-v1",
        "trust_configmap_name": "model-ca-v1",
        "model_projects": {"models-example": "model-invoke"},
    }
    with pytest.raises(ValueError, match="missing applied Terraform output"):
        _values(tmp_path, model_broker=broker)


def test_build_values_writes_the_range_telemetry_policy(tmp_path: Path):
    values = _values(tmp_path, range_telemetry={"nat_logs": {"filter": "ERRORS_ONLY"}})
    policy = json.loads(values["runtimeEnv"]["GCP_RANGE_TELEMETRY_POLICY_JSON"])
    assert policy["nat_logs"] == {"enabled": True, "filter": "ERRORS_ONLY"}
    assert policy["vpc_flow_logs"]["enabled"] is True
    assert policy["vertex_data_access_logs"] == {"enabled": True}


def test_build_values_writes_the_configured_lease_policy(tmp_path: Path):
    lease = {"initial_days": 7, "extension_days": 3, "maximum_days": 90, "extensions_enabled": False}
    values = _values(tmp_path, mission_control_leases=lease)
    assert json.loads(values["runtimeEnv"]["MISSION_CONTROL_LEASE_POLICY_JSON"]) == lease


@pytest.mark.parametrize(
    ("profile_id", "participants", "portal_replicas", "guacd_replicas"),
    [
        ("gcp-shared-v1-p10", 10, 2, 1),
        ("gcp-shared-v1-p30", 30, 5, 2),
        ("gcp-shared-v1-p50", 50, 8, 4),
        ("gcp-shared-v1-p100", 100, 14, 8),
    ],
)
def test_build_values_projects_every_selected_capacity_profile(
    tmp_path: Path, profile_id, participants, portal_replicas, guacd_replicas
):
    """One validated selector drives every Terraform-adjacent Helm shape (#1816)."""
    values = _values(tmp_path, shared_service_capacity_profile=profile_id)
    assert values["capacityProfile"] == {"id": profile_id, "participants": participants}
    assert values["portal"]["replicas"] == portal_replicas
    assert values["guacd"]["replicas"] == guacd_replicas
    assert values["guacamoleClient"]["replicas"] == 1
    assert values["runtimeEnv"]["SHARED_SERVICE_CAPACITY_PROFILE"] == profile_id
    assert values["runtimeEnv"]["PORTAL_WEB_WORKERS"] == "4"
    assert values["portal"]["terminationGracePeriodSeconds"] == 330
    assert values["guacamoleClient"]["postgresqlAbsoluteMaxConnections"] == participants
    assert values["services"]["portal"]["backendConfig"]["timeoutSec"] == 3600


def test_build_values_refuses_an_insecure_public_edge(tmp_path: Path):
    outputs = sample_gcp_outputs(PROJECT)
    outputs["managed_tls_enabled"] = {"value": False}
    with pytest.raises(ValueError, match="managed_tls"):
        _values(tmp_path, outputs=outputs)
