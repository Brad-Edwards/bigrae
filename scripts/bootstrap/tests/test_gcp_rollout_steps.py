"""Tests for the GCP rollout steps: runtime env, Helm, rollout checks, OpenVPN, smoke (#2571)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import gcp_helm
from bootstrap_core import get_repo_root
from gcp_control_plane import _load_python_script_module
from gcp_evidence import EvidenceRun
from gcp_helm import (
    MANAGED_BY_LABEL,
    RELEASE_NAME_ANNOTATION,
    RELEASE_NAMESPACE_ANNOTATION,
    HelmReleaseError,
    adopt_objects,
    build_values,
    helm_upgrade,
    owned_objects,
    profile_values_path,
    remove_retired_objects,
    render_chart,
    resource_type,
    write_values,
)
from gcp_inputs import load_deployment_inputs
from gcp_openvpn import OpenVpnError, ensure_pki, pool_output, roll_out_pool
from gcp_process import GcpCommandError, ProcessResult
from gcp_rollout import (
    ReleasePins,
    RolloutError,
    record_running_images,
    release_deployments,
    restart_workloads,
    wait_for_certificate,
    wait_for_ingress_address,
    wait_for_rollouts,
)
from gcp_runtime import compose_runtime_env, gdc_config, renderer_inputs
from gcp_smoke import run_smoke
from tests.gcp_test_support import PROJECT, REVISION, FakeRunner, root_config, sample_gcp_outputs, write_bundle


@pytest.fixture
def inputs(tmp_path: Path):
    config = root_config(
        range_cells={"zone": "us-central1-a", "project_id": "acme-range-cells", "host_identity_pool_size": 2},
        participant_openvpn={"enabled": False},
    )
    return load_deployment_inputs(str(write_bundle(tmp_path / "b", config=config)), tmp_path / "w", FakeRunner(), {})


def _identities(project: str = PROJECT) -> dict[str, str]:
    roots = sample_gcp_outputs(project)["artifact_registry_image_roots"]["value"]
    keys = {
        "portal": "portal",
        "provisioner": "pulumi-provisioner",
        "guacd": "guacd",
        "guacamole-client": "guacamole-client",
        "openvpn": "openvpn",
    }
    return {name: f"{roots[key]}@sha256:{str(index) * 64}" for index, (name, key) in enumerate(keys.items(), 1)}


class TestRuntimeEnv:
    def test_renderer_inputs_are_the_closed_deployment_projection(self, inputs):
        outputs = sample_gcp_outputs(PROJECT)
        outputs["range_host_service_account_email"] = {"value": "range-host@acme-shifter.iam.gserviceaccount.com"}
        values = renderer_inputs(inputs, outputs)
        assert values["RANGE_NETWORK_ZONE"] == "us-central1-a"
        assert values["GCP_RANGE_CELL_PROJECT_ID"] == "acme-range-cells"
        assert values["GCP_RANGE_LINUX_IMAGE"].endswith("shifter-ubuntu-aaaa")
        assert values["GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL"].startswith("range-host@")
        assert values["PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS"] == "ops@example.com"
        config = gdc_config(inputs)
        assert (config.project_id, config.environment, config.range_backend) == (PROJECT, "acme", "gce")

    def test_compose_runs_the_renderer_in_a_clean_environment(self, inputs):
        def render(argv, env, stdin):
            Path(argv[argv.index("--output") + 1]).write_text("CLOUD_PROVIDER=gcp\nGCP_RANGE_BACKEND=gce\nX=renderer\n")
            return ProcessResult(0)

        runner = FakeRunner(base={"PATH": "/bin", "HOME": "/h", "OPERATOR_SECRET": "leak"}).on([], render)
        runtime = compose_runtime_env(
            runner, inputs, sample_gcp_outputs(PROJECT), provisioner_digest="sha256:" + "2" * 64, workdir=inputs.workdir
        )
        call = runner.calls[0]
        assert "OPERATOR_SECRET" not in call.env and call.env["PATH"] == "/bin"
        assert call.env["RANGE_NETWORK_ZONE"] == "us-central1-a"
        assert call.argv[call.argv.index("--engine-image-digest") + 1] == "sha256:" + "2" * 64
        assert runtime["X"] == "renderer"
        assert runtime["ENVIRONMENT"] == "acme"
        assert runtime["GCP_RANGE_BACKEND"] == "gce"


class TestHelm:
    def _runtime_env(self, outputs) -> dict[str, str]:
        renderer = _load_python_script_module(get_repo_root() / "scripts/gcp/render_runtime_env.py", "test_render")
        rendered = renderer.render_env(outputs, engine_image="sha256:" + "2" * 64)
        return dict(line.split("=", 1) for line in rendered.splitlines() if line)

    def test_build_values_uses_exact_identities_and_the_profile_file(self, inputs, tmp_path: Path):
        outputs = sample_gcp_outputs(PROJECT)
        values = build_values(inputs, outputs, runtime_env=self._runtime_env(outputs), identities=_identities())
        assert values["images"] == {
            "platform": _identities()["portal"],
            "guacd": _identities()["guacd"],
            "guacamoleClient": _identities()["guacamole-client"],
        }
        assert values["runtimeEnv"]["CLOUD_PROVIDER"] == "gcp"
        assert profile_values_path("prod").name == "values-gcp-prod.yaml"
        with pytest.raises(HelmReleaseError):
            profile_values_path("staging")
        path = write_values(tmp_path, values)
        assert path.stat().st_mode & 0o777 == 0o600 and json.loads(path.read_text())["images"]

    def test_owned_objects_skip_hooks_and_name_qualified_types(self):
        documents = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "portal-web", "namespace": "shifter-platform"},
            },
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "platform-runtime", "namespace": "shifter-platform"},
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "m", "annotations": {"helm.sh/hook": "pre-upgrade"}},
            },
            {"kind": "Nameless", "metadata": {}},
        ]
        assert owned_objects(documents) == [
            ("deployment.apps", "shifter-platform", "portal-web"),
            ("configmap", "shifter-platform", "platform-runtime"),
        ]
        assert resource_type({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy"}) == (
            "networkpolicy.networking.k8s.io"
        )

    def test_adoption_labels_unowned_objects_and_refuses_foreign_releases(self):
        documents = [
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "a", "namespace": "ns"}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "b", "namespace": "ns"}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "c", "namespace": "ns"}},
        ]
        owned = {
            "labels": {MANAGED_BY_LABEL: "Helm"},
            "annotations": {RELEASE_NAME_ANNOTATION: "shifter", RELEASE_NAMESPACE_ANNOTATION: "shifter-system"},
        }
        live = {"items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b", **owned}}]}
        runner = FakeRunner().on(["kubectl", "get"], ProcessResult(0, json.dumps(live)))
        assert adopt_objects(runner, {}, documents) == ["deployment.apps/a"]
        label = runner.find("kubectl", "label")[0].argv
        assert label[2:5] == ["deployment.apps/a", "-n", "ns"]
        annotate = runner.find("kubectl", "annotate")[0].argv
        assert f"{RELEASE_NAME_ANNOTATION}=shifter" in annotate
        foreign = {"items": [{"metadata": {"name": "a", "annotations": {RELEASE_NAME_ANNOTATION: "other"}}}]}
        with pytest.raises(HelmReleaseError, match="another Helm release"):
            adopt_objects(FakeRunner().on(["kubectl", "get"], ProcessResult(0, json.dumps(foreign))), {}, documents)
        missing_kind = FakeRunner().on(["kubectl", "get"], ProcessResult(1))
        with pytest.raises(GcpCommandError):
            adopt_objects(missing_kind, {}, documents)

    def test_render_upgrade_and_retire(self):
        rendered = yaml.safe_dump_all([{"kind": "A", "metadata": {"name": "x"}}, None])
        runner = FakeRunner().on(["helm", "template"], ProcessResult(0, rendered))
        assert render_chart(runner, {}, [Path("/v1"), Path("/v2")]) == [{"kind": "A", "metadata": {"name": "x"}}]
        assert runner.calls[0].argv[-4:] == ["--values", "/v1", "--values", "/v2"]
        helm_upgrade(runner, {}, [Path("/v1")])
        upgrade = runner.find("helm", "upgrade")[0].argv
        assert upgrade == [
            "helm", "upgrade", "--install", "shifter", str(gcp_helm.chart_path()),
            "--namespace", "shifter-system", "--create-namespace", "--values", "/v1",
            "--atomic", "--wait", "--timeout", "15m", "--history-max", "10",
        ]  # fmt: skip

        def get(argv, env, stdin):
            name = argv[2].rsplit("/", 1)[1]
            if name == "platform-runtime-secrets":
                return ProcessResult(0, json.dumps({"metadata": {"annotations": {RELEASE_NAME_ANNOTATION: "shifter"}}}))
            if name == "allow-jobs-google-apis-egress":
                return ProcessResult(1)
            return ProcessResult(0, json.dumps({"metadata": {}}))

        retire = FakeRunner().on(["kubectl", "get"], get)
        removed = remove_retired_objects(retire, {})
        assert "secret/platform-runtime-secrets" not in removed
        assert removed == ["networkpolicy.networking.k8s.io/allow-platform-google-apis-egress"]
        assert len(retire.find("kubectl", "delete")) == 1
        retired_names = {name for _, _, name in gcp_helm.RETIRED_KUSTOMIZE_OBJECTS}
        assert retired_names.isdisjoint({"allow-portal-to-guacamole-client", "allow-portal-egress-to-guacamole-client"})


class TestRolloutChecks:
    def test_restarts_inventory_and_waits(self):
        runner = FakeRunner()
        restart_workloads(runner, {}, model_broker_enabled=True)
        restarted = [call.argv[3] for call in runner.find("kubectl", "rollout", "restart")]
        assert restarted[0] == "deployment/portal-web" and restarted[-1] == "deployment/model-access-control"
        with pytest.raises(RolloutError):
            release_deployments(FakeRunner(), {}, model_broker_enabled=False)
        pods = iter(
            [
                json.dumps({"items": [{"metadata": {"deletionTimestamp": "now"}}]}),
                json.dumps({"items": [{"metadata": {}}]}),
            ]
        )
        waiter = (
            FakeRunner()
            .on(["kubectl", "get", "pods"], lambda a, e, s: ProcessResult(0, next(pods)))
            .on(
                [],
                lambda a, e, s: (
                    ProcessResult(0, "portal-web\nguacd\n") if "--list-deployments" in a else ProcessResult(0)
                ),
            )
        )
        sleeps = []
        wait_for_rollouts(waiter, {}, model_broker_enabled=False, sleep=sleeps.append, clock=lambda: 0.0)
        assert [call.argv[3] for call in waiter.find("kubectl", "rollout", "status")] == [
            "deployment/portal-web",
            "deployment/guacd",
        ]
        assert sleeps == [5]
        stuck = (
            FakeRunner()
            .on(
                ["kubectl", "get", "pods"],
                ProcessResult(0, json.dumps({"items": [{"metadata": {"deletionTimestamp": "x"}}]})),
            )
            .on([], lambda a, e, s: ProcessResult(0, "portal-web\n") if "--list-deployments" in a else ProcessResult(0))
        )
        ticks = iter([0.0, 400.0])
        with pytest.raises(RolloutError, match="superseded"):
            wait_for_rollouts(stuck, {}, model_broker_enabled=False, sleep=lambda _: None, clock=lambda: next(ticks))

    def test_running_images_are_verified_and_stored(self, tmp_path: Path):
        def verifier(argv, env, stdin):
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text("{}\n")
            return ProcessResult(0, "[]" if argv[:3] == ["kubectl", "get", "pods"] else "")

        runner = FakeRunner().on([], verifier)
        record = record_running_images(
            runner,
            {},
            identities=_identities(),
            model_broker_enabled=False,
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 1),
            workdir=tmp_path,
        )
        assert record.prefix == f"deployments/{REVISION}/r/1"
        verify = next(call.argv for call in runner.calls if "--expected-image" in call.argv)
        assert f"portal={_identities()['portal']}" in verify
        assert "--support-image" not in verify
        proxy = "gcr.io/cloud-sql-connectors/cloud-sql-proxy@sha256:" + "4" * 64
        record_running_images(
            runner,
            {},
            identities=_identities(),
            model_broker_enabled=False,
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 2),
            workdir=tmp_path,
            pins=ReleasePins(support={"cloud-sql-proxy": proxy}),
        )
        verify = [call.argv for call in runner.calls if "--expected-image" in call.argv][-1]
        assert verify[verify.index("--support-image") + 1] == f"cloud-sql-proxy={proxy}"
        failing = FakeRunner().on(["kubectl"], ProcessResult(0, "[]")).on([], ProcessResult(1))
        with pytest.raises(RolloutError, match="approved release"):
            record_running_images(
                failing,
                {},
                identities=_identities(),
                model_broker_enabled=False,
                evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 1),
                workdir=tmp_path,
            )

    def test_support_images_come_from_the_rendered_release_deployments(self):
        """#2698: one pinned proxy reference per release; a second one is an error."""
        proxy = "gcr.io/cloud-sql-connectors/cloud-sql-proxy@sha256:" + "4" * 64

        def deployment(name, image, namespace="shifter-platform"):
            sidecar = {"name": "cloud-sql-proxy", "image": image, "restartPolicy": "Always"}
            return {
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {"template": {"spec": {"initContainers": [sidecar], "containers": [{"name": name}]}}},
            }

        documents = [deployment("portal-web", proxy), deployment("worker-cms", proxy), {"kind": "Service"}]
        assert ReleasePins.from_documents(documents).support == {"cloud-sql-proxy": proxy}
        assert ReleasePins.from_documents([{"kind": "Service", "metadata": {}}]) == ReleasePins()
        # Another namespace is outside the verified release boundary.
        assert ReleasePins.from_documents([deployment("x", proxy + "0", namespace="other"), *documents]).support == {
            "cloud-sql-proxy": proxy
        }
        with pytest.raises(RolloutError, match="more than one image"):
            ReleasePins.from_documents([*documents, deployment("worker-mc", proxy.replace("4", "5"))])

    def test_policy_image_comes_from_the_rendered_policy_servers(self, tmp_path: Path):
        """#2698: the OpenFGA servers join the running image check at their pinned image."""
        image = "docker.io/openfga/openfga@sha256:" + "6" * 64

        def server(name):
            return {
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": "shifter-platform"},
                "spec": {"template": {"spec": {"containers": [{"name": "openfga", "image": image}]}}},
            }

        assert ReleasePins.from_documents([server("openfga"), server("openfga-admin")]).policy == image
        assert ReleasePins.from_documents([{"kind": "Service", "metadata": {}}]).policy is None

        def verifier(argv, env, stdin):
            if "--output" in argv:
                Path(argv[argv.index("--output") + 1]).write_text("{}\n")
            return ProcessResult(0, "[]" if argv[:3] == ["kubectl", "get", "pods"] else "")

        runner = FakeRunner().on([], verifier)
        record_running_images(
            runner,
            {},
            identities=_identities(),
            model_broker_enabled=False,
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 3),
            workdir=tmp_path,
            pins=ReleasePins(policy=image),
        )
        verify = next(call.argv for call in runner.calls if "--expected-image" in call.argv)
        assert verify[verify.index("--policy-enabled") + 1] == "true"
        assert f"openfga={image}" in verify

    def test_ingress_and_certificate_waits(self):
        addresses = iter([ProcessResult(0, ""), ProcessResult(0, "34.1.2.3")])
        runner = FakeRunner().on(["kubectl", "get", "ingress"], lambda a, e, s: next(addresses))
        assert wait_for_ingress_address(runner, {}, sleep=lambda _: None) == "34.1.2.3"
        with pytest.raises(RolloutError):
            wait_for_ingress_address(
                FakeRunner().on(["kubectl"], ProcessResult(1)), {}, attempts=2, sleep=lambda _: None
            )
        cert = {"spec": {"domains": ["portal.acme.example"]}, "status": {"certificateStatus": "Provisioning"}}
        provisioning = FakeRunner().on(["kubectl"], ProcessResult(0, json.dumps(cert)))
        assert (
            wait_for_certificate(
                provisioning, {}, hostname="portal.acme.example", require_active=False, sleep=lambda _: None
            )
            is False
        )
        active = dict(cert, status={"certificateStatus": "Active"})
        ready = FakeRunner().on(["kubectl"], ProcessResult(0, json.dumps(active)))
        assert wait_for_certificate(
            ready, {}, hostname="portal.acme.example", require_active=True, sleep=lambda _: None
        )
        assert wait_for_certificate(
            ready, {}, hostname="portal.acme.example", require_active=False, sleep=lambda _: None
        )
        absent = FakeRunner().on(["kubectl"], ProcessResult(1))
        with pytest.raises(RolloutError):
            wait_for_certificate(absent, {}, hostname="portal.acme.example", require_active=False, sleep=lambda _: None)
        with pytest.raises(RolloutError):
            wait_for_certificate(
                provisioning, {}, hostname="portal.acme.example", require_active=True, sleep=lambda _: None
            )


POOL = {
    "issuer_secret": "projects/p/secrets/vpn-issuer",
    "server_secret": "projects/p/secrets/vpn-server",
    "release_secret": "projects/p/secrets/vpn-release",
    "instance_group": "shifter-acme-vpn",
    "region": "us-central1",
    "backend_service": "shifter-acme-vpn-backend",
    "image_root": "us-central1-docker.pkg.dev/acme-shifter/shifter-acme-openvpn/openvpn",
}


class TestOpenVpn:
    def test_pool_output_and_pki(self, tmp_path: Path):
        assert pool_output({}) is None
        assert pool_output({"openvpn_pool": {"value": POOL}}) == POOL
        with pytest.raises(OpenVpnError):
            pool_output({"openvpn_pool": {"value": ["x"]}})

        def pki(argv, env, stdin):
            Path(argv[argv.index("--changed-output") + 1]).write_text("changed=true\n")
            return ProcessResult(0)

        runner = FakeRunner().on([], pki)
        assert ensure_pki(runner, {}, pool=POOL, project_id=PROJECT, workdir=tmp_path) is True
        assert "vpn-issuer" in runner.calls[0].argv

    def test_rollout_records_the_release_and_waits_for_health(self):
        health = json.dumps([{"status": {"healthStatus": [{"healthState": "HEALTHY"}]}}])
        runner = (
            FakeRunner()
            .on(["gcloud", "secrets", "versions", "access"], ProcessResult(0, "old-release"))
            .on(["gcloud", "compute", "backend-services"], ProcessResult(0, health))
            .on(
                ["gcloud", "secrets", "versions", "list"],
                ProcessResult(
                    0, "projects/p/secrets/vpn-release/versions/3\nprojects/p/secrets/vpn-release/versions/2\n"
                ),
            )
        )
        digest = "sha256:" + "5" * 64
        roll_out_pool(
            runner,
            {},
            pool=POOL,
            image=f"{POOL['image_root']}@{digest}",
            project_id=PROJECT,
            pki_changed=False,
            sleep=lambda _: None,
        )
        added = runner.find("gcloud", "secrets", "versions", "add")[0]
        assert added.input_text == f"{POOL['image_root']}@{digest}"
        assert runner.find("gcloud", "compute", "instance-groups", "managed", "rolling-action")
        destroyed = runner.find("gcloud", "secrets", "versions", "destroy")
        assert [call.argv[4] for call in destroyed] == ["2"]

    def test_rollout_guards(self):
        with pytest.raises(OpenVpnError, match="digest"):
            roll_out_pool(
                FakeRunner(),
                {},
                pool=POOL,
                image=f"{POOL['image_root']}@latest",
                project_id=PROJECT,
                pki_changed=False,
            )
        with pytest.raises(OpenVpnError, match="image root"):
            roll_out_pool(
                FakeRunner(),
                {},
                pool=POOL,
                image="other@sha256:" + "5" * 64,
                project_id=PROJECT,
                pki_changed=False,
            )
        current = f"{POOL['image_root']}@sha256:" + "5" * 64
        unhealthy = (
            FakeRunner()
            .on(["gcloud", "secrets", "versions", "access"], ProcessResult(0, current))
            .on(["gcloud", "compute", "backend-services"], ProcessResult(0, "[]"))
        )
        with pytest.raises(OpenVpnError, match="healthy"):
            roll_out_pool(
                unhealthy,
                {},
                pool=POOL,
                image=current,
                project_id=PROJECT,
                pki_changed=False,
                sleep=lambda _: None,
            )
        assert not unhealthy.find("gcloud", "compute", "instance-groups", "managed", "rolling-action")
        denied = FakeRunner().on(["gcloud", "secrets", "versions", "access"], ProcessResult(1, "", "PERMISSION_DENIED"))
        with pytest.raises(OpenVpnError, match="release record"):
            roll_out_pool(
                denied,
                {},
                pool=POOL,
                image=current,
                project_id=PROJECT,
                pki_changed=False,
            )


class TestSmoke:
    def test_smoke_paths(self):
        assert run_smoke(FakeRunner(), {}, user_email="", run_id="r") is True
        runner = FakeRunner()
        assert run_smoke(runner, {"K": "1"}, user_email="smoke@example.com", run_id="20261028-abc") is True
        call = runner.calls[0]
        assert call.argv[0] == "bash" and call.argv[-2:] == ["--variant", "linux"]
        assert call.env["SMOKE_TEST_USER_EMAIL"] == "smoke@example.com" and call.env["SMOKE_RUN_ID"] == "20261028-abc"
        assert (
            run_smoke(FakeRunner().on(["bash"], ProcessResult(1)), {}, user_email="s@example.com", run_id="r") is False
        )
        with pytest.raises(ValueError):
            run_smoke(FakeRunner(), {}, user_email="s@example.com", run_id="r", variant="mac")


@pytest.mark.parametrize("bad", ["not-json", "{}", '{"items":[{}]}', None])
def test_adoption_validates_every_inventory_before_any_metadata_effect(bad):
    documents = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a", "namespace": "ns"}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "b", "namespace": "ns"}},
    ]
    runner = FakeRunner().on(["kubectl", "get", "configmap"], ProcessResult(0, '{"items":[{"metadata":{"name":"a"}}]}'))
    runner.on(["kubectl", "get", "deployment.apps"], ProcessResult(1) if bad is None else ProcessResult(0, bad))
    with pytest.raises((GcpCommandError, HelmReleaseError)):
        adopt_objects(runner, {}, documents)
    assert all(call.argv[:2] == ["kubectl", "get"] for call in runner.calls)
