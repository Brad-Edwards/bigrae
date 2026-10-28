"""Tests for the GCP deployment-input settings that replace GitHub variables (#2571)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from installation.settings_gcp import GcpBackendSettings
from installation.settings_gcp_deploy import (
    GcpOperatorBootstrapSettings,
    GcpParticipantOpenVpnSettings,
    GcpRangeCellSettings,
    deployment_runtime_env,
    deployment_terraform_values,
)


def _settings(**overrides: object) -> dict[str, object]:
    return {
        "project_id": "acme-shifter",
        "dynamic_secret_project_id": "acme-range-secrets",
        "region": "us-central1",
        **overrides,
    }


FULL_DEPLOYMENT = {
    "range_cells": {
        "backend": "gce",
        "zone": "us-central1-a",
        "zones": ["us-central1-a", "us-east1-b"],
        "project_id": "acme-range-cells",
        "host_identity_pool_size": 24,
        "private_google_access": False,
        "image_key_profiles": {"windows": {"image": "projects/acme-shifter/global/images/win"}},
    },
    "participant_openvpn": {"enabled": True},
    "ctf_content_bucket": "acme-ctf-content",
    "operator_bootstrap": {
        "admin_email": "Ops@Example.com",
        "admin_password_secret_ref": "projects/acme-shifter/secrets/operator-password",
        "staff_emails": ["staff@example.com", "ops@example.com"],
        "superuser_emails": ["root@example.com"],
    },
    "post_deploy_smoke": {"user_email": "smoke@example.com"},
    "release_trust": {"github_repository_id": "1234", "github_owner_id": "5678"},
}


class TestDeploymentSettings:
    def test_defaults_keep_existing_configs_valid(self):
        settings = GcpBackendSettings.model_validate(_settings())
        assert settings.range_cells == GcpRangeCellSettings()
        assert settings.participant_openvpn.enabled is False
        assert settings.ctf_content_bucket == ""
        assert settings.operator_bootstrap is None
        assert settings.post_deploy_smoke.user_email == ""
        assert settings.release_trust is None

    def test_full_deployment_inputs_project_onto_runtime_and_terraform(self):
        settings = GcpBackendSettings.model_validate(_settings(**FULL_DEPLOYMENT))
        env = deployment_runtime_env(settings.range_cells, settings.participant_openvpn, settings.operator_bootstrap)
        assert env == {
            "GCP_RANGE_BACKEND": "gce",
            "GCP_RANGE_HOST_IDENTITY_POOL_SIZE": "24",
            "RANGE_OPENVPN_ENABLED": "true",
            "RANGE_NETWORK_ZONE": "us-central1-a",
            "RANGE_NETWORK_ZONES": "us-central1-a,us-east1-b",
            "GCP_RANGE_CELL_PROJECT_ID": "acme-range-cells",
            "GCP_RANGE_PRIVATE_GOOGLE_ACCESS": "false",
            "GCP_RANGE_IMAGE_KEY_PROFILES_JSON": json.dumps(
                FULL_DEPLOYMENT["range_cells"]["image_key_profiles"], sort_keys=True, separators=(",", ":")
            ),
            # The first operator joins both elevation lists once, case-insensitively.
            "PLATFORM_BOOTSTRAP_STAFF_EMAILS": "staff@example.com,ops@example.com",
            "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS": "root@example.com,ops@example.com",
        }
        assert deployment_terraform_values(
            settings.range_cells, settings.participant_openvpn, settings.ctf_content_bucket
        ) == {
            "openvpn_pool_enabled": True,
            "range_host_identity_pool_size": 24,
            "range_network_zones": ["us-central1-a", "us-east1-b"],
            "ctf_content_bucket_name": "acme-ctf-content",
        }
        assert settings.release_trust is not None
        assert settings.release_trust.github_repository_id == "1234"

    def test_unset_optional_inputs_keep_renderer_defaults(self):
        env = deployment_runtime_env(GcpRangeCellSettings(), GcpParticipantOpenVpnSettings(), None)
        assert env == {
            "GCP_RANGE_BACKEND": "gce",
            "GCP_RANGE_HOST_IDENTITY_POOL_SIZE": "0",
            "RANGE_OPENVPN_ENABLED": "false",
        }
        disabled = GcpOperatorBootstrapSettings(enabled=False, staff_emails=["a@example.com"])
        env = deployment_runtime_env(GcpRangeCellSettings(), GcpParticipantOpenVpnSettings(), disabled)
        assert env["PLATFORM_BOOTSTRAP_STAFF_EMAILS"] == "a@example.com"
        assert env["PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS"] == ""
        assert deployment_terraform_values(GcpRangeCellSettings(), GcpParticipantOpenVpnSettings(), "") == {
            "openvpn_pool_enabled": False,
            "range_host_identity_pool_size": 0,
            "range_network_zones": [],
        }

    @pytest.mark.parametrize(
        "overrides",
        [
            {"range_cells": {"zone": "us-central1"}},
            {"range_cells": {"zones": ["us-central1-a", "us-central1-a"]}},
            {"range_cells": {"host_identity_pool_size": -1}},
            {"range_cells": {"backend": "aws"}},
            {"range_cells": {"project_id": "Bad_Project"}},
            {"range_cells": {"unknown": True}},
            {"range_cells": {"image_key_profiles": {"k": "x" * 40000}}},
            {"ctf_content_bucket": "Bad Bucket"},
            {"operator_bootstrap": {"enabled": True}},
            {"operator_bootstrap": {"admin_email": "not-an-email"}},
            {"operator_bootstrap": {"admin_email": "ops@example.com", "admin_password_secret_ref": "hunter2"}},
            {"operator_bootstrap": {"admin_email": "ops@example.com", "staff_emails": ["a@example.com,b@x.com"]}},
            {"post_deploy_smoke": {"user_email": "nobody"}},
            {"release_trust": {"github_repository_id": "0123", "github_owner_id": "5"}},
            {"release_trust": {"github_repository_id": "123"}},
            {"participant_openvpn": {"enabled": True, "extra": 1}},
        ],
    )
    def test_invalid_deployment_inputs_fail_closed(self, overrides: dict[str, object]):
        with pytest.raises(ValidationError):
            GcpBackendSettings.model_validate(_settings(**overrides))

    def test_operator_bootstrap_can_be_disabled_deliberately(self):
        settings = GcpBackendSettings.model_validate(_settings(operator_bootstrap={"enabled": False}))
        assert settings.operator_bootstrap is not None
        assert settings.operator_bootstrap.enabled is False

    def test_from_root_settings_keeps_the_new_blocks(self):
        root_settings = {**_settings(**FULL_DEPLOYMENT), "range_egress": {"mode": "status-quo"}}
        rebuilt = GcpBackendSettings.from_root_settings(root_settings)
        assert rebuilt.range_cells.zone == "us-central1-a"
        assert rebuilt.post_deploy_smoke.user_email == "smoke@example.com"
