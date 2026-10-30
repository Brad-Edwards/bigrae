"""Tests for the gcp-deploy, gcp-release-scan, gcp-smoke, gcp-destroy, and gcp-config commands (#2571)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pytest

import gcp_cli
import gcp_deploy
import gcp_deploy_stages
import gcp_destroy
from gcp_deploy import (
    DeployOptions,
    GcpDeployError,
    ScanOptions,
    check_tooling_revision,
    evidence_bucket,
    load_manifest,
    new_run_id,
    release_trust,
    run_deploy,
    run_release_scan_command,
    run_smoke_command,
    tooling_revision,
)
from gcp_deploy_stages import (
    DeployStageError,
    InfrastructureResult,
    gateway_context,
    reconcile_foundation_image_network,
    require_tools,
    retire_inventoried_artifacts,
    sync_policy_credentials,
    verify_range_telemetry,
)
from gcp_destroy import DestroyOptions, run_destroy
from gcp_inputs import load_deployment_inputs
from gcp_process import ProcessResult
from tests.gcp_test_support import (
    PROJECT,
    REVISION,
    FakeRunner,
    release_images,
    root_config,
    sample_gcp_outputs,
    write_bundle,
    write_manifest,
)

IMPERSONATION_KEY = "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return write_bundle(tmp_path / "bundle")


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    return write_manifest(tmp_path / "release.json")


@pytest.fixture
def stages(monkeypatch):
    """Replace the stage implementations and record the identity each one ran under."""
    seen: list[tuple[str, str | None]] = []
    outputs = sample_gcp_outputs(PROJECT)

    def infra(runner, inputs, **kwargs):
        seen.append(("infrastructure", os.environ.get(IMPERSONATION_KEY)))
        return inputs, InfrastructureResult(outputs=outputs, kube={"KUBECONFIG": "/work/kubeconfig"})

    def read(runner, inputs):
        seen.append(("read", os.environ.get(IMPERSONATION_KEY)))
        return InfrastructureResult(outputs=outputs, kube={"KUBECONFIG": "/work/kubeconfig"})

    monkeypatch.setattr(gcp_deploy, "infrastructure_stage", infra)
    monkeypatch.setattr(gcp_deploy, "read_infrastructure", read)
    monkeypatch.setattr(
        gcp_deploy_stages, "check_range_preconditions", lambda *a, **k: seen.append(("preconditions", None))
    )
    monkeypatch.setattr(
        gcp_deploy,
        "_import_images",
        lambda runner, inputs, infra, manifest, source: gcp_deploy.source_images(manifest, source),
    )
    monkeypatch.setattr(
        gcp_deploy,
        "_scan",
        lambda runner, images, evidence, workdir: seen.append(("scan", os.environ.get(IMPERSONATION_KEY))),
    )
    monkeypatch.setattr(
        gcp_deploy, "_verify", lambda runner, images, trust: seen.append(("verify", os.environ.get(IMPERSONATION_KEY)))
    )
    monkeypatch.setattr(
        gcp_deploy,
        "rollout_stage",
        lambda runner, inputs, infra, request: seen.append(("rollout", os.environ.get(IMPERSONATION_KEY))) or True,
    )
    monkeypatch.setattr(
        gcp_deploy,
        "run_smoke",
        lambda runner, env, **kwargs: seen.append(("smoke", os.environ.get("KUBECONFIG"))) or True,
    )
    monkeypatch.setattr(gcp_deploy, "require_tools", lambda *names: None)
    monkeypatch.setattr(gcp_deploy, "resolve_operator_password", lambda *a, **k: "pw")
    monkeypatch.setenv(gcp_deploy.SOURCE_REVISION_ENV, REVISION)
    return seen


class TestRunDeploy:
    def test_full_operator_deploy_runs_each_stage_as_its_purpose_identity(self, bundle, manifest, stages, monkeypatch):
        runner = FakeRunner().on(["gcloud", "storage", "objects", "describe"], ProcessResult(0, "7\n"))
        options = DeployOptions(source="gs://acme-shifter-deployment-config", release_manifest=str(manifest))
        status_runner = runner

        def fetch(*args, **kwargs):
            return load_deployment_inputs(str(bundle), Path(args[1]), FakeRunner(), {})

        monkeypatch.setattr(gcp_deploy, "load_deployment_inputs", fetch)
        assert run_deploy(options, runner=status_runner) == 0
        deploy = f"shifteracme-deploy@{PROJECT}.iam.gserviceaccount.com"
        scan = f"shifteracme-scan@{PROJECT}.iam.gserviceaccount.com"
        # The release images verify before any change, and the imported copies verify before rollout.
        assert [name for name, _ in stages] == [
            "verify",
            "infrastructure",
            "preconditions",
            "scan",
            "verify",
            "rollout",
            "smoke",
        ]
        identities = dict(stages)
        assert identities["infrastructure"] == deploy
        assert identities["scan"] == scan
        assert identities["verify"] == identities["rollout"] == deploy
        assert identities["smoke"] == "/work/kubeconfig"
        assert runner.find("gcloud", "storage", "cp")[0].argv[-1].endswith("/locks/deploy.lock")
        assert "--if-generation-match=7" in runner.find("gcloud", "storage", "rm")[0].argv
        assert IMPERSONATION_KEY not in os.environ

    def test_ci_stages_use_ambient_identity_and_an_external_lock(self, bundle, manifest, stages, tmp_path: Path):
        roots = tmp_path / "image-roots.json"
        options = DeployOptions(
            source=str(bundle),
            stage="infrastructure",
            identity_mode="ambient",
            lock="external",
            operator_password_env="PW",
            image_roots_output=str(roots),
        )
        assert run_deploy(options, runner=FakeRunner()) == 0
        assert stages == [("infrastructure", None)]
        written = json.loads(roots.read_text())
        assert sorted(written) == ["guacamole-client", "guacd", "openvpn", "portal", "provisioner"]
        assert written["provisioner"].endswith("/pulumi-provisioner")
        assert roots.stat().st_mode & 0o777 == 0o600
        stages.clear()
        rollout = DeployOptions(
            source=str(bundle),
            stage="rollout",
            identity_mode="ambient",
            lock="external",
            release_manifest=str(manifest),
            trusted_repository_id="1234",
            trusted_owner_id="5678",
        )
        assert run_deploy(rollout, runner=FakeRunner()) == 0
        assert [name for name, _ in stages] == ["read", "preconditions", "verify", "rollout"]
        assert all(identity is None for _, identity in stages)

    def test_ci_rollout_needs_no_operator_password_and_fewer_tools(self, bundle, manifest, stages, monkeypatch):
        """Only the infrastructure stage seeds the operator; the rollout reads no password."""
        monkeypatch.setattr(gcp_deploy, "resolve_operator_password", lambda *a, **k: pytest.fail("read a password"))
        required: list[tuple[str, ...]] = []
        monkeypatch.setattr(gcp_deploy, "require_tools", lambda *names: required.append(names))
        rollout = DeployOptions(
            source=str(bundle),
            stage="rollout",
            identity_mode="ambient",
            lock="external",
            release_manifest=str(manifest),
            trusted_repository_id="1234",
            trusted_owner_id="5678",
        )
        assert run_deploy(rollout, runner=FakeRunner()) == 0
        assert required == [("gcloud", "gke-gcloud-auth-plugin", "terraform", "kubectl", "helm")]

    def test_smoke_failure_returns_exit_status_three(self, bundle, manifest, stages, monkeypatch):
        monkeypatch.setattr(gcp_deploy, "run_smoke", lambda *a, **k: False)
        options = DeployOptions(
            source=str(bundle), lock="external", release_manifest=str(manifest), identity_mode="ambient"
        )
        assert run_deploy(options, runner=FakeRunner()) == 3

    def test_operator_stages_cannot_skip_the_release_gates(self, bundle, manifest, stages, monkeypatch):
        """ADR-070-R5/R6: an operator rollout scans, and an operator infrastructure run verifies the release."""
        required: list[tuple[str, ...]] = []
        monkeypatch.setattr(gcp_deploy, "require_tools", lambda *names: required.append(names))
        rollout = DeployOptions(source=str(bundle), stage="rollout", lock="external", release_manifest=str(manifest))
        assert run_deploy(rollout, runner=FakeRunner()) == 0
        assert [name for name, _ in stages] == ["verify", "read", "preconditions", "scan", "verify", "rollout"]
        assert {"oras", "trivy"} <= set(required[0])
        stages.clear()
        infrastructure = DeployOptions(
            source=str(bundle), stage="infrastructure", lock="external", release_manifest=str(manifest)
        )
        assert run_deploy(infrastructure, runner=FakeRunner()) == 0
        assert [name for name, _ in stages] == ["verify", "infrastructure"]
        assert "trivy" not in required[1]
        monkeypatch.delenv(gcp_deploy.DEFAULT_RELEASE_MANIFEST_ENV, raising=False)
        with pytest.raises(GcpDeployError, match="release manifest is required"):
            run_deploy(DeployOptions(source=str(bundle), stage="infrastructure", lock="external"), runner=FakeRunner())

    def test_release_source_mirror_is_verified_and_imported(self, bundle, manifest, stages, monkeypatch):
        """#2659: with a mirror, the gates verify and import the mirror copy of each release digest."""
        verified: list[dict[str, str]] = []
        monkeypatch.setattr(gcp_deploy, "_verify", lambda runner, images, trust: verified.append(dict(images)))
        options = DeployOptions(
            source=str(bundle),
            stage="rollout",
            lock="external",
            release_manifest=str(manifest),
            release_source="mirror.example/team/bigrae",
        )
        assert run_deploy(options, runner=FakeRunner()) == 0
        assert len(verified) == 2
        for images in verified:
            assert images
            assert all(
                identity.startswith(f"mirror.example/team/bigrae/{name}@sha256:") for name, identity in images.items()
            )
        with pytest.raises(RuntimeError, match="registry prefix"):
            run_deploy(
                DeployOptions(
                    source=str(bundle),
                    stage="rollout",
                    lock="external",
                    release_manifest=str(manifest),
                    release_source="https://mirror.example",
                ),
                runner=FakeRunner(),
            )

    def test_rejects_bad_stage_local_lock_and_mismatched_tooling(self, bundle, manifest, stages, monkeypatch):
        with pytest.raises(GcpDeployError, match="--stage"):
            run_deploy(DeployOptions(source=str(bundle), stage="partial"), runner=FakeRunner())
        with pytest.raises(GcpDeployError, match="--store"):
            run_deploy(DeployOptions(source=str(bundle), release_manifest=str(manifest)), runner=FakeRunner())
        monkeypatch.setenv(gcp_deploy.SOURCE_REVISION_ENV, "f" * 40)
        with pytest.raises(GcpDeployError, match="does not match"):
            run_deploy(
                DeployOptions(source=str(bundle), release_manifest=str(manifest), lock="external"), runner=FakeRunner()
            )


class TestDeployHelpers:
    def test_run_id_and_evidence_bucket(self):
        run_id = new_run_id()
        assert run_id[:14].isdigit() and len(run_id) == 23 and run_id == run_id.lower()
        assert evidence_bucket(DeployOptions(source="x"), PROJECT) == f"{PROJECT}-release-evidence"
        assert evidence_bucket(DeployOptions(source="x", evidence_bucket="custom"), PROJECT) == "custom"

    def test_tooling_revision_from_the_image_or_a_clean_checkout(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(gcp_deploy.SOURCE_REVISION_ENV, REVISION)
        assert tooling_revision(tmp_path) == (REVISION, True)
        check_tooling_revision(REVISION, repo_root=tmp_path)
        monkeypatch.delenv(gcp_deploy.SOURCE_REVISION_ENV)
        repo = tmp_path / "repo"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        (repo / "a.txt").write_text("1\n")
        subprocess.run([*git, "add", "a.txt"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init", "--no-gpg-sign"], check=True)
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        assert tooling_revision(repo) == (head, True)
        (repo / "a.txt").write_text("2\n")
        with pytest.raises(GcpDeployError, match="local changes"):
            check_tooling_revision(head, repo_root=repo)

    def test_manifest_and_trust_resolution(self, bundle, manifest, tmp_path: Path, monkeypatch):
        assert load_manifest(str(manifest)).source_revision == REVISION
        monkeypatch.delenv(gcp_deploy.DEFAULT_RELEASE_MANIFEST_ENV, raising=False)
        with pytest.raises(GcpDeployError, match="release manifest is required"):
            load_manifest("")
        monkeypatch.setenv(gcp_deploy.DEFAULT_RELEASE_MANIFEST_ENV, str(manifest))
        assert load_manifest("").version == "1.4.0"
        broken = tmp_path / "broken.json"
        broken.write_text("{}")
        with pytest.raises(GcpDeployError):
            load_manifest(str(broken))
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        trust = release_trust(inputs, DeployOptions(source="x"), REVISION)
        assert (trust.repository_id, trust.owner_id) == ("1234", "5678")
        with pytest.raises(GcpDeployError, match="conflict"):
            release_trust(inputs, DeployOptions(source="x", trusted_repository_id="9", trusted_owner_id="9"), REVISION)
        config = root_config()
        del config["settings"]["release_trust"]
        untrusted = load_deployment_inputs(
            str(write_bundle(tmp_path / "u", config=config)), tmp_path / "w2", FakeRunner(), {}
        )
        with pytest.raises(GcpDeployError, match="release_trust is required"):
            release_trust(untrusted, DeployOptions(source="x"), REVISION)
        assert release_trust(
            untrusted, DeployOptions(source="x", trusted_repository_id="7", trusted_owner_id="8"), REVISION
        )
        # A stage that needs the release stops when the run holds none (fail closed).
        with pytest.raises(GcpDeployError, match="requires the release manifest"):
            gcp_deploy._required_release(None, trust)
        assert gcp_deploy._required_release(load_manifest(str(manifest)), trust)[1] is trust

    def test_require_tools(self, monkeypatch):
        monkeypatch.setattr(gcp_deploy_stages, "tool_available", lambda name: name != "helm")
        require_tools("gcloud")
        with pytest.raises(DeployStageError, match="helm"):
            require_tools("gcloud", "helm")


class TestStageHelpers:
    def test_context_name_and_optional_steps(self, bundle, tmp_path: Path):
        assert (
            gateway_context(PROJECT, "us-central1", "shifter-acme-gke")
            == f"connectgateway_{PROJECT}_us-central1_shifter-acme-gke"
        )
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        runner = FakeRunner()
        retire_inventoried_artifacts(runner, inputs, "ctx")
        assert runner.calls == []
        assert sync_policy_credentials(runner, inputs, {"native_identity": {"value": {"openfga_enabled": False}}}) is False
        assert (
            sync_policy_credentials(runner, inputs, {"native_identity": {"value": {"openfga_enabled": True}}}) is True
        )
        # The three derived OpenFGA secrets exist (#2679), so nothing is generated before the sync.
        describes = runner.find("gcloud", "secrets", "versions", "describe")
        assert [call.argv[call.argv.index("--secret") + 1] for call in describes] == [
            "shifter-acme-openfga-runtime",
            "shifter-acme-openfga-admin",
            "shifter-acme-openfga-ca",
        ]
        assert not runner.find("gcloud", "secrets", "create")
        assert "sync_openfga_secrets.py" in runner.calls[-1].argv[1]
        with pytest.raises(DeployStageError, match="foundation-inputs"):
            reconcile_foundation_image_network(runner, inputs)

    def test_foundation_reconcile_and_telemetry_readback(self, tmp_path: Path):
        bundle = write_bundle(
            tmp_path / "b",
            extra={"foundation-inputs.json": '{"name_prefix": "shifter-acme"}', "retired-cluster-artifacts.json": "{}"},
        )
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        runner = FakeRunner()
        reconcile_foundation_image_network(runner, inputs)
        assert json.loads(runner.calls[0].env["GCP_FOUNDATION_INPUTS_JSON"]) == {"name_prefix": "shifter-acme"}
        retire_inventoried_artifacts(runner, inputs, "ctx")
        assert runner.calls[1].argv[-4:-2] == ["--context", "ctx"]
        verify_range_telemetry(runner, inputs, sample_gcp_outputs(PROJECT))
        telemetry = runner.calls[2].argv
        assert telemetry[telemetry.index("--range-network") + 1] == "shifter-gcp-dev-range"
        assert "--terraform-nat-router" in telemetry
        with pytest.raises(DeployStageError):
            verify_range_telemetry(runner, inputs, {})


class TestOtherCommands:
    def test_release_scan_command_uses_only_the_scan_identity(self, manifest, monkeypatch):
        seen = []
        monkeypatch.setattr(gcp_deploy, "require_tools", lambda *n: None)
        monkeypatch.setattr(
            gcp_deploy,
            "_scan",
            lambda runner, images, evidence, workdir: seen.append((os.environ.get(IMPERSONATION_KEY), evidence.bucket)),
        )
        options = ScanOptions(release_manifest=str(manifest), project_id=PROJECT, environment="acme")
        assert run_release_scan_command(options, runner=FakeRunner()) == 0
        assert seen == [(f"shifteracme-scan@{PROJECT}.iam.gserviceaccount.com", f"{PROJECT}-release-evidence")]
        scanned = []
        monkeypatch.setattr(gcp_deploy, "_scan", lambda runner, images, evidence, workdir: scanned.append(images))
        mirror = ScanOptions(
            release_manifest=str(manifest), project_id=PROJECT, environment="acme", release_source="m.example/x"
        )
        assert run_release_scan_command(mirror, runner=FakeRunner()) == 0
        assert all(identity.startswith(f"m.example/x/{name}@") for name, identity in scanned[0].items())

    def test_smoke_command(self, bundle, monkeypatch):
        monkeypatch.setattr(gcp_deploy, "require_tools", lambda *n: None)
        monkeypatch.setattr(
            gcp_deploy, "read_infrastructure", lambda runner, inputs: InfrastructureResult(outputs={}, kube={})
        )
        monkeypatch.setattr(gcp_deploy, "run_smoke", lambda *a, **k: False)
        assert run_smoke_command(DeployOptions(source=str(bundle), identity_mode="ambient"), runner=FakeRunner()) == 3

    def test_destroy_requires_confirmation_and_verified_tooling(self, bundle, manifest, monkeypatch):
        steps = []
        monkeypatch.setattr(gcp_destroy, "require_tools", lambda *n: None)
        monkeypatch.setattr(
            gcp_destroy, "_verify_release_tooling", lambda runner, options, inputs: steps.append("verified")
        )
        for name in (
            "terraform_init",
            "apply_destroy_settings",
            "drop_unremovable_kms_resources",
            "terraform_destroy",
        ):
            monkeypatch.setattr(
                f"gcp_terraform.{name}", lambda *a, _n=name, **k: steps.append((_n, os.environ.get(IMPERSONATION_KEY)))
            )
        monkeypatch.setattr(
            gcp_destroy,
            "remove_platform_release",
            lambda *a, **k: steps.append(("remove_platform_release", os.environ.get(IMPERSONATION_KEY))),
        )
        with pytest.raises(GcpDeployError, match="--confirm"):
            run_destroy(DestroyOptions(source=str(bundle), confirm="other", lock="external"), runner=FakeRunner())
        assert (
            run_destroy(DestroyOptions(source=str(bundle), confirm="acme", lock="external"), runner=FakeRunner()) == 0
        )
        destroy = f"shifteracme-destroy@{PROJECT}.iam.gserviceaccount.com"
        assert steps[0] == "verified"
        assert [step[0] for step in steps[1:]] == [
            "terraform_init",
            "remove_platform_release",
            "apply_destroy_settings",
            "drop_unremovable_kms_resources",
            "terraform_destroy",
        ]
        assert all(step[1] == destroy for step in steps[1:])
        steps.clear()
        run_destroy(
            DestroyOptions(source=str(bundle), confirm="acme", lock="external", identity_mode="ambient"),
            runner=FakeRunner(),
        )
        assert "verified" not in steps

    def test_destroy_uninstalls_the_release_before_terraform(self, bundle, tmp_path: Path):
        # The Ingress controller removes its load balancer only while the cluster exists (#2640).
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        absent = FakeRunner().on(["gcloud", "container", "fleet", "memberships", "describe"], ProcessResult(1))
        gcp_destroy.remove_platform_release(absent, inputs)
        assert not absent.find("helm")
        no_release = FakeRunner().on(["helm", "status"], ProcessResult(1))
        gcp_destroy.remove_platform_release(no_release, inputs)
        assert no_release.find("helm", "status") and not no_release.find("helm", "uninstall")
        installed = FakeRunner()
        gcp_destroy.remove_platform_release(installed, inputs)
        uninstall = installed.find("helm", "uninstall")[0].argv
        assert uninstall == [
            "helm",
            "uninstall",
            "shifter",
            "--namespace",
            "shifter-system",
            "--wait",
            "--timeout",
            "15m",
        ]
        assert installed.find("gcloud", "container", "fleet", "memberships", "get-credentials")

    def test_destroy_tooling_verification_checks_release_provenance(
        self, bundle, manifest, monkeypatch, tmp_path: Path
    ):
        verified = []
        monkeypatch.setenv(gcp_deploy.SOURCE_REVISION_ENV, REVISION)
        monkeypatch.setattr(
            "gcp_provenance.verify_release_provenance", lambda images, trust, **k: verified.append((images, trust))
        )
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        gcp_destroy._verify_release_tooling(
            FakeRunner(), DestroyOptions(source=str(bundle), confirm="acme", release_manifest=str(manifest)), inputs
        )
        images, trust = verified[0]
        assert images == release_images() and trust.source_revision == REVISION
        mirror = DestroyOptions(
            source=str(bundle), confirm="acme", release_manifest=str(manifest), release_source="mirror.example/bigrae"
        )
        gcp_destroy._verify_release_tooling(FakeRunner(), mirror, inputs)
        assert all(identity.startswith(f"mirror.example/bigrae/{name}@") for name, identity in verified[1][0].items())


class TestCli:
    def test_parser_builds_options_and_dispatch_reports_failures(self, monkeypatch, capsys):
        parser = argparse.ArgumentParser()
        gcp_cli.add_lifecycle_subparsers(parser.add_subparsers(dest="command"))
        args = parser.parse_args(
            [
                "gcp-deploy",
                "--store",
                "gs://b",
                "--stage",
                "rollout",
                "--identity",
                "ambient",
                "--no-smoke",
                "--first-install",
                "--release-source",
                "mirror.example/bigrae",
            ]
        )
        options = gcp_cli._deploy_options(args)
        assert (options.source, options.stage, options.identity_mode, options.smoke, options.first_install) == (
            "gs://b",
            "rollout",
            "ambient",
            False,
            True,
        )
        assert options.release_source == "mirror.example/bigrae"
        for command in (
            ["gcp-destroy", "--store", "gs://b", "--confirm", "acme"],
            ["gcp-release-scan", "--release-manifest", "m", "--project-id", "p", "--environment", "e"],
        ):
            assert parser.parse_args([*command, "--release-source", "m.example/x"]).release_source == "m.example/x"

        def failing(args):
            raise GcpDeployError("bad input")

        monkeypatch.setitem(gcp_cli._HANDLERS, "gcp-deploy", failing)
        with pytest.raises(SystemExit) as exit_info:
            gcp_cli.dispatch_lifecycle(args)
        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "bad input" in captured.out + captured.err
        calls = []
        monkeypatch.setattr(gcp_cli, "publish", lambda store, inputs: calls.append(("publish", store, inputs)))
        monkeypatch.setattr(gcp_cli, "write_range_images", lambda store, images: calls.append(("image", store, images)))
        gcp_cli._handle_config(parser.parse_args(["gcp-config", "publish", "--store", "gs://b", "--inputs", "/d"]))
        gcp_cli._handle_config(
            parser.parse_args(
                [
                    "gcp-config",
                    "set-range-image",
                    "--store",
                    "gs://b",
                    "--role",
                    "dc",
                    "--image",
                    "projects/p/global/images/x",
                ]
            )
        )
        assert calls == [("publish", "gs://b", "/d"), ("image", "gs://b", {"dc": "projects/p/global/images/x"})]
