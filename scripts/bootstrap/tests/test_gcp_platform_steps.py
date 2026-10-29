"""Tests for the GCP deploy Terraform, project prerequisite, and kube steps (#2571)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib import error as urllib_error

import pytest

import gcp_terraform
from gcp_inputs import load_deployment_inputs
from gcp_kube import apply_manifest, cluster_name, kube_env, kubectl_json
from gcp_prereqs import (
    STATE_RETENTION_SECONDS,
    PrerequisiteError,
    ensure_identity_platform,
    ensure_state_bucket,
    release_disabled_broker_load_balancer,
)
from gcp_process import ProcessResult
from gcp_terraform import (
    TerraformStepError,
    artifact_registry_kms_adoption,
    backend_prefix,
    deployment_values,
    drop_unremovable_kms_resources,
    lift_sql_deletion_protection,
    owned_keys_in_values,
    state_bucket_name,
    terraform_apply,
    terraform_context,
    terraform_destroy,
    terraform_init,
    terraform_outputs,
)
from tests.gcp_test_support import PROJECT, FakeRunner, root_config, write_bundle


@pytest.fixture
def inputs(tmp_path: Path):
    config = root_config(
        range_cells={"zone": "us-central1-a", "zones": ["us-central1-a"], "host_identity_pool_size": 3},
        participant_openvpn={"enabled": True},
        ctf_content_bucket="acme-ctf",
    )
    values = {
        "identity_allowed_email_domain": "example.com",
        "project_id": "REPLACE",
        "gke_master_authorized_cidrs": [],
    }
    return load_deployment_inputs(
        str(write_bundle(tmp_path / "bundle", config=config, terraform_values=values)),
        tmp_path / "work",
        FakeRunner(),
        {},
    )


class TestTerraformInputs:
    def test_root_config_owns_its_values_and_wins_last(self, inputs):
        values = deployment_values(inputs)
        assert values["project_id"] == PROJECT
        assert values["environment"] == "acme"
        assert values["public_hostname"] == "portal.acme.example"
        assert values["artifact_registry_location"] == "us-central1"
        assert values["enable_managed_tls"] is True
        assert values["openvpn_pool_enabled"] is True
        assert values["range_network_zones"] == ["us-central1-a"]
        assert values["range_host_identity_pool_size"] == 3
        assert values["ctf_content_bucket_name"] == "acme-ctf"
        assert "gcp_bootstrap_admin_password" not in values
        destroy = deployment_values(inputs, destroy=True)
        assert destroy["cloud_sql_deletion_protection"] is False and destroy["gcs_force_destroy"] is True

    def test_context_renders_private_var_files_in_precedence_order(self, inputs):
        ctx = terraform_context(inputs, inputs.workdir, {"GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": "d@x"})
        names = [path.name for path in ctx.var_files]
        assert names == ["terraform.tfvars.json", "settings.auto.tfvars", "zz-deployment.auto.tfvars.json"]
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in ctx.var_files)
        assert "dynamic_secret_project_id" in ctx.var_files[1].read_text()
        assert ctx.env["TF_DATA_DIR"] == str(inputs.workdir / "terraform-data")
        assert ctx.env["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"] == "d@x"
        assert ctx.argv("plan")[:2] == ["terraform", f"-chdir={ctx.root}"]
        assert owned_keys_in_values(ctx.var_files[0]) == ["project_id"]

    def test_hcl_and_json_values_keep_terraform_order(self, tmp_path: Path):
        bundle = write_bundle(tmp_path / "b")
        (bundle / "terraform.tfvars").write_text('gke_master_authorized_cidrs = ["0.0.0.0/0"]\n')
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        with pytest.raises(ValueError, match="world-open"):
            terraform_context(inputs, inputs.workdir, {})
        (bundle / "terraform.tfvars.json").write_text(
            '{"identity_allowed_email_domain": "example.com", "gke_master_authorized_cidrs": []}'
        )
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w2", FakeRunner(), {})
        names = [path.name for path in terraform_context(inputs, inputs.workdir, {}).var_files]
        assert names[:2] == ["terraform.tfvars", "terraform.tfvars.json"]

    def test_owned_keys_are_detected_in_hcl(self, tmp_path: Path):
        hcl = tmp_path / "terraform.tfvars"
        hcl.write_text('environment = "orthanc"\nweb_node_count = 2\n# public_hostname = "x"\n')
        assert owned_keys_in_values(hcl) == ["environment"]

    def test_insecure_authorized_networks_fail_before_terraform(self, tmp_path: Path):
        values = {"identity_allowed_email_domain": "example.com", "gke_master_authorized_cidrs": ["0.0.0.0/0"]}
        inputs = load_deployment_inputs(
            str(write_bundle(tmp_path / "b", terraform_values=values)), tmp_path / "w", FakeRunner(), {}
        )
        with pytest.raises(ValueError, match="world-open"):
            terraform_context(inputs, inputs.workdir, {})

    def test_names(self):
        assert state_bucket_name(PROJECT) == f"{PROJECT}-terraform-state"
        assert backend_prefix("acme") == "shifter/acme/platform-core"


class TestTerraformSteps:
    def test_init_apply_and_outputs(self, inputs):
        ctx = terraform_context(inputs, inputs.workdir, {})
        runner = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "output"], ProcessResult(0, '{"a": {"value": 1}}'))
        terraform_init(runner, ctx, state_bucket="b", prefix="p")
        assert runner.argvs()[0][-2:] == ["-backend-config=bucket=b", "-backend-config=prefix=p"]
        terraform_apply(runner, ctx)
        # The platform root adopts the Identity Platform config with an import block (#2598).
        assert not runner.find("terraform", f"-chdir={ctx.root}", "import")
        assert terraform_outputs(runner, ctx) == {"a": {"value": 1}}

    def test_apply_retries_only_iam_propagation(self, inputs, capsys):
        ctx = terraform_context(inputs, inputs.workdir, {})
        attempts = iter([ProcessResult(1, "", "Error 403: permission denied"), ProcessResult(0, "done")])
        runner = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "apply"], lambda a, e, s: next(attempts))
        sleeps = []
        terraform_apply(runner, ctx, sleep=sleeps.append)
        assert sleeps == [10]
        # The retried error stays visible to the operator (#2601).
        assert "Error 403: permission denied" in capsys.readouterr().out
        hard = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "apply"], ProcessResult(1, "", "invalid config"))
        with pytest.raises(TerraformStepError):
            terraform_apply(hard, ctx, sleep=sleeps.append)

    def test_outputs_must_be_a_json_object(self, inputs):
        ctx = terraform_context(inputs, inputs.workdir, {})
        for body in ("not json", "[]"):
            with pytest.raises(TerraformStepError):
                terraform_outputs(FakeRunner().on(["terraform"], ProcessResult(0, body)), ctx)

    def test_destroy_steps(self, inputs):
        ctx = terraform_context(inputs, inputs.workdir, {}, destroy=True)
        state = "\n".join(
            [
                "module.platform_core.module.portal_cloud_sql.google_sql_database_instance.platform",
                "module.platform_core.module.portal_artifact_registry.google_kms_key_ring.artifact_registry",
                "module.platform_core.module.portal_artifact_registry.google_kms_crypto_key.artifact_registry",
            ]
        )
        runner = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "state", "list"], ProcessResult(0, state))
        lift_sql_deletion_protection(runner, ctx)
        drop_unremovable_kms_resources(runner, ctx)
        targeted = runner.find("terraform", f"-chdir={ctx.root}", "apply")[0].argv
        assert targeted[-1].startswith("-target=") and targeted[-1].endswith("google_sql_database_instance.platform")
        removed = [call.argv[-1] for call in runner.find("terraform", f"-chdir={ctx.root}", "state", "rm")]
        assert [address.rsplit(".", 2)[-2] for address in removed] == ["google_kms_crypto_key", "google_kms_key_ring"]
        attempts = iter([ProcessResult(1), ProcessResult(0)])
        retrying = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "destroy"], lambda a, e, s: next(attempts))
        sleeps = []
        terraform_destroy(retrying, ctx, sleep=sleeps.append)
        assert sleeps == [60]
        always = FakeRunner().on(["terraform", f"-chdir={ctx.root}", "destroy"], ProcessResult(1))
        with pytest.raises(TerraformStepError):
            terraform_destroy(always, ctx, sleep=lambda _: None)
        empty = FakeRunner()
        lift_sql_deletion_protection(empty, ctx)
        assert not empty.find("terraform", f"-chdir={ctx.root}", "apply")

    def test_reinstall_adopts_the_key_ring_that_a_destroy_left(self, inputs):
        # gcp-destroy drops the key ring from state; a reinstall adopts it (#2643).
        ctx = terraform_context(inputs, inputs.workdir, {})
        assert artifact_registry_kms_adoption(FakeRunner(), ctx, inputs) == ("-var=adopt_artifact_registry_kms=true",)
        absent = FakeRunner().on(["gcloud", "kms", "keyrings", "describe"], ProcessResult(1))
        assert artifact_registry_kms_adoption(absent, ctx, inputs) == ()
        managed = FakeRunner().on(
            ["terraform", f"-chdir={ctx.root}", "state", "list"],
            ProcessResult(
                0, "module.platform_core.module.portal_artifact_registry.google_kms_key_ring.artifact_registry\n"
            ),
        )
        assert artifact_registry_kms_adoption(managed, ctx, inputs) == ()
        assert not managed.find("gcloud", "kms")

    def test_state_list_failure_is_an_empty_state(self, inputs):
        ctx = terraform_context(inputs, inputs.workdir, {})
        assert gcp_terraform.state_addresses(FakeRunner().on(["terraform"], ProcessResult(1)), ctx) == []


RETENTION_FORMAT = "--format=value(retentionPolicy.retentionPeriod)"


class _Resp(io.BytesIO):
    def __init__(self, status=200):
        super().__init__(b"{}")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPrerequisites:
    def _runner(self) -> FakeRunner:
        return FakeRunner().on(["gcloud", "auth", "print-access-token"], ProcessResult(0, "tok\n"))

    def test_identity_platform_initializes_once(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url))
            if request.get_method() == "GET":
                raise urllib_error.HTTPError(request.full_url, 404, "nf", None, None)
            return _Resp()

        ensure_identity_platform(self._runner(), {}, project_id=PROJECT, opener=opener)
        assert calls[-1][1].endswith(f"/v2/projects/{PROJECT}/identityPlatform:initializeAuth")
        ensure_identity_platform(self._runner(), {}, project_id=PROJECT, opener=lambda r, timeout: _Resp())

        def failing(request, timeout):
            raise urllib_error.HTTPError(
                request.full_url, 404 if request.get_method() == "GET" else 403, "x", None, None
            )

        with pytest.raises(PrerequisiteError, match="initializeAuth"):
            ensure_identity_platform(self._runner(), {}, project_id=PROJECT, opener=failing)

        def down(request, timeout):
            raise urllib_error.URLError("down")

        with pytest.raises(PrerequisiteError, match="unreachable"):
            ensure_identity_platform(self._runner(), {}, project_id=PROJECT, opener=down)
        with pytest.raises(PrerequisiteError, match="no access token"):
            ensure_identity_platform(
                FakeRunner().on(["gcloud", "auth"], ProcessResult(0, "")), {}, project_id=PROJECT, opener=down
            )

    def test_state_bucket_is_created_hardened_and_cleaned(self):
        policy = {"bindings": [{"role": "roles/storage.objectViewer", "members": ["allUsers"]}]}
        runner = (
            FakeRunner()
            .on(
                ["gcloud", "storage", "buckets", "describe", "gs://b", "--project", PROJECT, RETENTION_FORMAT],
                ProcessResult(0, ""),
            )
            .on(["gcloud", "storage", "buckets", "describe"], ProcessResult(1))
            .on(["gcloud", "storage", "buckets", "get-iam-policy"], ProcessResult(0, json.dumps(policy)))
        )
        ensure_state_bucket(runner, {}, bucket="b", project_id=PROJECT, region="us-central1")
        argvs = runner.argvs()
        assert any(argv[:4] == ["gcloud", "storage", "buckets", "create"] for argv in argvs)
        assert any(f"--retention-period={STATE_RETENTION_SECONDS}s" in argv for argv in argvs)
        removal = runner.find("gcloud", "storage", "buckets", "remove-iam-policy-binding")[0].argv
        assert "--member=allUsers" in removal

    def test_existing_strict_bucket_keeps_its_retention(self):
        runner = (
            FakeRunner()
            .on(
                ["gcloud", "storage", "buckets", "describe", "gs://b", "--project", PROJECT, RETENTION_FORMAT],
                ProcessResult(0, str(STATE_RETENTION_SECONDS * 2)),
            )
            .on(["gcloud", "storage", "buckets", "get-iam-policy"], ProcessResult(0, "{}"))
        )
        ensure_state_bucket(runner, {}, bucket="b", project_id=PROJECT, region="us-central1")
        assert not any("--retention-period" in " ".join(argv) for argv in runner.argvs())
        assert not runner.find("gcloud", "storage", "buckets", "create")
        broken = FakeRunner().on(["gcloud", "storage", "buckets", "get-iam-policy"], ProcessResult(0, "not json"))
        with pytest.raises(PrerequisiteError):
            ensure_state_bucket(broken, {}, bucket="b", project_id=PROJECT, region="us-central1")

    def test_disabled_broker_load_balancer_release(self):
        runner = FakeRunner()
        release_disabled_broker_load_balancer(
            runner, {}, broker_enabled=True, project_id=PROJECT, region="r", cluster="c", kube_env={}
        )
        assert runner.calls == []
        absent = FakeRunner().on(["gcloud", "container", "fleet"], ProcessResult(1))
        release_disabled_broker_load_balancer(
            absent, {}, broker_enabled=False, project_id=PROJECT, region="r", cluster="c", kube_env={}
        )
        assert not absent.find("kubectl")
        present = FakeRunner()
        release_disabled_broker_load_balancer(
            present, {}, broker_enabled=False, project_id=PROJECT, region="r", cluster="c", kube_env={"KUBECONFIG": "k"}
        )
        assert present.find("kubectl", "delete", "service", "model-broker")[0].env["KUBECONFIG"] == "k"
        failing = FakeRunner().on(["kubectl"], ProcessResult(1))
        with pytest.raises(PrerequisiteError):
            release_disabled_broker_load_balancer(
                failing, {}, broker_enabled=False, project_id=PROJECT, region="r", cluster="c", kube_env={}
            )


class TestKube:
    def test_isolated_kubeconfig_and_helpers(self, tmp_path: Path):
        runner = FakeRunner()
        env = kube_env(runner, {"X": "1"}, workdir=tmp_path, project_id=PROJECT, location="us-central1", cluster="c")
        assert env["KUBECONFIG"] == str(tmp_path / "kube" / "config")
        assert env["USE_GKE_GCLOUD_AUTH_PLUGIN"] == "True" and env["X"] == "1"
        assert runner.calls[0].argv[:6] == ["gcloud", "container", "fleet", "memberships", "get-credentials", "c"]
        assert runner.calls[0].env["KUBECONFIG"] == env["KUBECONFIG"]
        assert cluster_name("acme") == "shifter-acme-gke"
        assert kubectl_json(FakeRunner().on(["kubectl"], ProcessResult(0, '{"a": 1}')), {}, "get", "x") == {"a": 1}
        assert kubectl_json(FakeRunner().on(["kubectl"], ProcessResult(0, "[]")), {}, "get", "x") == {}
        assert kubectl_json(FakeRunner().on(["kubectl"], ProcessResult(0, "bad")), {}, "get", "x") == {}
        applier = FakeRunner()
        apply_manifest(applier, {}, {"kind": "Secret", "data": {"k": "v"}})
        assert applier.calls[0].argv == ["kubectl", "apply", "-f", "-"]
        assert json.loads(applier.calls[0].input_text)["kind"] == "Secret"
