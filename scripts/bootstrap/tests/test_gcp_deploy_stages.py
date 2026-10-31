"""Tests for the infrastructure and rollout stages of gcp-deploy (#2571)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import gcp_deploy_stages
from gcp_catalog import initialize_catalog as actual_initialize_catalog
from gcp_deploy_stages import (
    InfrastructureResult,
    RolloutRequest,
    apply_broker_overlay,
    check_range_preconditions,
    infrastructure_stage,
    read_infrastructure,
    restart_policy_clients,
    rollout_stage,
    seed_operator,
)
from gcp_evidence import EvidenceRun
from gcp_inputs import load_deployment_inputs
from gcp_process import ProcessResult
from tests.gcp_test_support import PROJECT, REVISION, FakeRunner, root_config, sample_gcp_outputs, write_bundle
from tests.test_gcp_identity_cutover import chart, facts

__all__ = ["chart", "facts"]


def activated_runner(events=None):
    def status(*args):
        if events is not None:
            events.append("verified_identity_probe")
        return ProcessResult(0, 'SHIFTER_IDENTITY_CUTOVER_RESULT={"state":"activated"}\n')

    return FakeRunner().on(["kubectl", "logs"], status)


@pytest.fixture
def inputs(tmp_path: Path):
    return load_deployment_inputs(str(write_bundle(tmp_path / "b")), tmp_path / "w", FakeRunner(), {})


@pytest.fixture
def recorder(monkeypatch, chart):
    """Patch every collaborator a stage imports and record the call order."""
    order: list[str] = []
    outputs = sample_gcp_outputs(PROJECT)

    def record(name, result=None):
        def call(*args, **kwargs):
            order.append(name)
            return result(*args, **kwargs) if callable(result) else result

        return call

    for module, names in {
        "gcp_catalog": ("initialize_catalog",),
        "gcp_prereqs": (
            "ensure_identity_platform",
            "ensure_state_bucket",
            "release_disabled_broker_load_balancer",
            "remove_deleted_role_bindings",
        ),
        "gcp_control_plane": (
            "ensure_gcp_artifact_registry_service_identity",
            "ensure_gcp_control_plane_namespaces",
            "run_gcp_database_migrations",
            "sync_gcp_guacamole_runtime_secret",
        ),
        "gcp_terraform": ("terraform_init", "terraform_apply"),
        "gcp_helm": ("adopt_objects", "helm_upgrade", "remove_retired_objects"),
        "gcp_openvpn": ("roll_out_pool",),
        "gcp_rollout": ("restart_workloads", "wait_for_rollouts", "record_running_images", "wait_for_ingress_address"),
    }.items():
        for name in names:
            monkeypatch.setattr(f"{module}.{name}", record(name))
    # The duty reconciliation before the probe is not a schema migration (#2704).
    monkeypatch.setattr(
        "gcp_control_plane.run_gcp_database_migrations",
        lambda *a, duties_only=False, **k: order.append(
            "reconcile_database_duties" if duties_only else "run_gcp_database_migrations"
        ),
    )
    monkeypatch.setattr("gcp_terraform.terraform_context", record("terraform_context", lambda *a, **k: object()))
    monkeypatch.setattr("gcp_terraform.terraform_outputs", record("terraform_outputs", lambda *a, **k: outputs))
    monkeypatch.setattr("gcp_terraform.artifact_registry_kms_adoption", lambda *a, **k: False)
    monkeypatch.setattr("gcp_helm.render_chart", record("render_chart", lambda *a, **k: chart))
    monkeypatch.setattr(
        "gcp_helm.build_values", record("build_values", lambda *a, **k: {"modelBroker": {"enabled": False}})
    )
    monkeypatch.setattr("gcp_helm.write_values", record("write_values", lambda *a, **k: Path("/work/values.json")))
    monkeypatch.setattr("gcp_helm.profile_values_path", lambda profile: Path(f"/chart/values-gcp-{profile}.yaml"))
    monkeypatch.setattr("gcp_runtime.compose_runtime_env", record("compose_runtime_env", lambda *a, **k: {}))
    monkeypatch.setattr("gcp_rollout.wait_for_certificate", record("wait_for_certificate", lambda *a, **k: True))
    monkeypatch.setattr("gcp_openvpn.ensure_pki", record("ensure_pki", lambda *a, **k: False))
    monkeypatch.setattr(
        gcp_deploy_stages, "kube_env", record("kube_env", lambda *a, **k: {"KUBECONFIG": "/work/kubeconfig"})
    )
    monkeypatch.setattr(gcp_deploy_stages, "verify_range_telemetry", record("verify_range_telemetry"))
    monkeypatch.setattr(gcp_deploy_stages, "seed_operator", record("seed_operator"))
    # The release model publication and the generated OpenFGA credentials (#2679).
    monkeypatch.setattr("gcp_policy.ensure_policy_secrets", record("ensure_policy_secrets", lambda *a, **k: False))
    monkeypatch.setattr(
        "gcp_policy.publish_policy_model",
        record("publish_policy_model", lambda *a, **k: ("01J00000000000000000000001", "01J00000000000000000000002")),
    )
    monkeypatch.setattr("gcp_policy.release_model", lambda model: {"type_definitions": [{"type": "principal"}]})
    monkeypatch.setattr(
        gcp_deploy_stages, "operator_bootstrap_facts", record("operator_bootstrap_facts", lambda *a, **k: None)
    )
    return order, outputs


class TestInfrastructureStage:
    def test_first_install_order_without_an_existing_cluster(self, inputs, recorder):
        order, outputs = recorder
        runner = FakeRunner().on(["gcloud", "container", "fleet", "memberships", "describe"], ProcessResult(1))
        updated, result = infrastructure_stage(runner, inputs, operator_password="pw", reconcile_foundation=False)
        assert updated is inputs
        assert result.outputs is outputs and result.kube == {"KUBECONFIG": "/work/kubeconfig"}
        assert order == [
            "ensure_identity_platform",
            "ensure_state_bucket",
            "remove_deleted_role_bindings",
            "ensure_gcp_artifact_registry_service_identity",
            "terraform_context",
            "terraform_init",
            "terraform_apply",
            "terraform_outputs",
            "verify_range_telemetry",
            "kube_env",
            "seed_operator",
        ]

    def test_update_releases_a_disabled_broker_before_apply(self, inputs, recorder):
        order, _ = recorder
        infrastructure_stage(FakeRunner(), inputs, operator_password="pw", reconcile_foundation=False)
        assert order.index("release_disabled_broker_load_balancer") < order.index("terraform_apply")
        assert order.count("kube_env") == 2

    def test_read_infrastructure_changes_nothing(self, inputs, recorder):
        order, outputs = recorder
        result = read_infrastructure(FakeRunner(), inputs)
        assert result.outputs is outputs
        assert order == ["terraform_context", "terraform_init", "terraform_outputs", "kube_env"]


class TestRolloutStage:
    def test_missing_catalog_proof_leaves_app_ready_and_prints_dns_recovery(
        self, inputs, recorder, monkeypatch, capsys
    ):
        order, outputs = recorder
        monkeypatch.setattr("gcp_catalog.initialize_catalog", actual_initialize_catalog)
        request = RolloutRequest(
            identities={
                name: f"host/{name}@sha256:" + "1" * 64
                for name in ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
            },
            evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
            first_install=True,
        )
        # #2679: pending catalog work is a warning, not a failed deploy.
        assert rollout_stage(activated_runner(), inputs, InfrastructureResult(outputs=outputs, kube={}), request)
        assert "catalog may be incomplete" in capsys.readouterr().out
        assert order.index("helm_upgrade") < order.index("wait_for_rollouts") < order.index("wait_for_ingress_address")
        assert order.index("wait_for_certificate") < order.index("operator_bootstrap_facts")

    def test_legacy_policy_migration_and_activation_precede_full_release_restore(self, inputs, recorder, facts):
        import yaml

        order, outputs = recorder
        outputs["native_identity"] = {"value": {"openfga_enabled": True}}

        def logs(argv, env, payload):
            if "identity-cutover-status-" in argv[2]:
                order.append("verified_legacy_probe")
                return ProcessResult(0, 'SHIFTER_IDENTITY_CUTOVER_RESULT={"state":"planned"}\n')
            order.append("verified_activation")
            return ProcessResult(0)

        def applied(argv, env, payload):
            for document in yaml.safe_load_all(payload):
                if (
                    document
                    and document.get("kind") == "Job"
                    and document["metadata"]["name"] == "openfga-datastore-migrate"
                ):
                    order.append("policy_schema")
            return ProcessResult(0)

        def policy_restart(*args):
            order.append("policy_restart")
            return ProcessResult(0)

        def disabled_compute(*args):
            order.append("retire_disabled_compute")
            return ProcessResult(0)

        runner = FakeRunner().on(["kubectl", "logs"], logs).on(["kubectl", "apply"], applied)
        runner.on(["kubectl", "rollout", "restart"], policy_restart)
        runner.on(["kubectl", "delete", "deployment"], disabled_compute)
        request = RolloutRequest(
            identities={
                name: f"host/{name}@sha256:" + "1" * 64
                for name in ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
            },
            evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
            first_install=False,
            identity_cutover_facts=str(facts[0]),
            identity_bootstrap_facts=str(facts[1]),
        )
        rollout_stage(runner, inputs, InfrastructureResult(outputs=outputs, kube={}), request)
        assert order.index("verified_legacy_probe") < order.index("run_gcp_database_migrations")
        assert order.index("run_gcp_database_migrations") < order.index("policy_schema")
        assert order.index("policy_schema") < order.index("policy_restart") < order.index("verified_activation")
        assert order.index("verified_activation") < order.index("retire_disabled_compute") < order.index("helm_upgrade")
        assert order.count("policy_restart") == 2

    def test_release_cannot_migrate_without_probing_identity(self, inputs, recorder):
        order, outputs = recorder
        runner = activated_runner(order)
        request = RolloutRequest(
            identities={
                name: f"host/{name}@sha256:" + "1" * 64
                for name in ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
            },
            evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
            first_install=False,
        )
        rollout_stage(runner, inputs, InfrastructureResult(outputs=outputs, kube={}), request)
        assert any("identity-cutover" in " ".join(call.argv) for call in runner.calls)
        assert order.index("verified_identity_probe") < order.index("run_gcp_database_migrations")
        assert order.index("verified_identity_probe") < order.index("adopt_objects")
        # Only the duty reconciliation precedes the probe (#2704).
        assert order.index("reconcile_database_duties") < order.index("verified_identity_probe")

    def test_rollout_order_and_identity_scope(self, inputs, recorder):
        order, outputs = recorder
        request = RolloutRequest(
            identities={
                name: f"host/{name}@sha256:" + "1" * 64
                for name in ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
            },
            evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
            first_install=False,
        )
        infra = InfrastructureResult(outputs=outputs, kube={"KUBECONFIG": "/work/kubeconfig"})
        runner = activated_runner(order).on(["kubectl", "get", "deployment", "openfga-admin"], ProcessResult(0, "1"))
        assert rollout_stage(runner, inputs, infra, request) is True
        # #2679: the deploy creates the OpenFGA credentials before the identity
        # probe and registers the catalog as the bootstrap administrator after
        # the certificate wait. #2694: an existing installation publishes the
        # release model and renders with its IDs before the probe checks them.
        # #2704: the SQL duties are reconciled before the probe connects as a
        # native login.
        assert order == [
            "compose_runtime_env",
            "build_values",
            "write_values",
            "render_chart",
            "ensure_gcp_control_plane_namespaces",
            "ensure_policy_secrets",
            "publish_policy_model",
            "build_values",
            "write_values",
            "render_chart",
            "reconcile_database_duties",
            "verified_identity_probe",
            "run_gcp_database_migrations",
            "sync_gcp_guacamole_runtime_secret",
            "adopt_objects",
            "helm_upgrade",
            "remove_retired_objects",
            "restart_workloads",
            "wait_for_rollouts",
            "record_running_images",
            "wait_for_ingress_address",
            "wait_for_certificate",
            "operator_bootstrap_facts",
            "initialize_catalog",
        ]
        assert "KUBECONFIG" not in os.environ or os.environ["KUBECONFIG"] != "/work/kubeconfig"

    def test_rollout_with_pool_and_policy(self, inputs, recorder, monkeypatch):
        order, outputs = recorder
        outputs["openvpn_pool"] = {"value": {"image_root": "host/openvpn"}}
        outputs["native_identity"] = {"value": {"openfga_enabled": True}}
        runner = activated_runner()
        request = RolloutRequest(
            identities={
                name: f"host/{name}@sha256:" + "1" * 64
                for name in ("portal", "provisioner", "guacd", "guacamole-client", "openvpn")
            },
            evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
            first_install=True,
        )
        rollout_stage(runner, inputs, InfrastructureResult(outputs=outputs, kube={}), request)
        assert "ensure_pki" in order and "roll_out_pool" in order
        # Without a running admin server, the model publishes after the migrations (#2694).
        assert order.index("run_gcp_database_migrations") < order.index("publish_policy_model")
        assert any("sync_openfga_secrets.py" in " ".join(call.argv) for call in runner.calls)
        assert runner.find("kubectl", "rollout", "restart", "deployment/openfga")


class TestStageHelpers:
    def test_broker_overlay_is_a_no_op_without_a_template(self, inputs):
        runner = FakeRunner()
        assert apply_broker_overlay(runner, inputs, mode="read", context="ctx") is inputs
        assert runner.calls == []

    def test_broker_overlay_binds_and_reloads_the_config(self, tmp_path: Path):
        template = '{"settings": {}}'
        bundle = write_bundle(tmp_path / "b", extra={"model-broker-overlay.template.json": template})
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        runner = FakeRunner()
        updated = apply_broker_overlay(runner, inputs, mode="ensure", context="ctx")
        assert updated.environment == inputs.environment
        prepare, apply = runner.calls
        assert prepare.argv[prepare.argv.index("--mode") + 1] == "ensure"
        assert apply.env["SHIFTER_CONFIG_OVERLAY_FILE"].endswith("model-broker-overlay.json")
        assert "SHIFTER_CONFIG_OVERLAY_JSON" not in apply.env

    def test_preconditions_use_the_deployment_projection(self, inputs, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "gcp_control_plane.check_gce_range_preconditions",
            lambda config, allow_missing_range_images, env: seen.update(env=env, allow=allow_missing_range_images),
        )
        check_range_preconditions(inputs, sample_gcp_outputs(PROJECT), allow_missing=True)
        assert seen["allow"] is True and seen["env"]["RANGE_NETWORK_ZONE"] == "us-central1-a"

    def test_gdc_backend_skips_the_gce_preconditions(self, tmp_path: Path, monkeypatch):
        config = root_config(range_cells={"backend": "gdc"})
        inputs = load_deployment_inputs(
            str(write_bundle(tmp_path / "g", config=config)), tmp_path / "w", FakeRunner(), {}
        )
        monkeypatch.setattr("gcp_control_plane.check_gce_range_preconditions", lambda *a, **k: pytest.fail("ran"))
        check_range_preconditions(inputs, {}, allow_missing=False)

    def test_seed_operator_passes_explicit_credentials(self, inputs, tmp_path: Path, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "gcp_control_plane.ensure_gcp_identity_platform_operator",
            lambda config, outputs, credentials: seen.update(credentials=credentials),
        )
        seed_operator(inputs, {}, password="pw")
        assert seen["credentials"] == ("ops@example.com", "pw")
        disabled = load_deployment_inputs(
            str(write_bundle(tmp_path / "d", config=root_config(operator_bootstrap={"enabled": False}))),
            tmp_path / "w2",
            FakeRunner(),
            {},
        )
        seen.clear()
        seed_operator(disabled, {}, password="")
        assert seen == {}

    def test_policy_client_restarts(self):
        runner = FakeRunner()
        restart_policy_clients(runner)
        assert [call.argv[2] for call in runner.calls] == ["restart", "restart", "status", "status"]
