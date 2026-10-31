"""Tests for the one-command GitHub-less install (#2679)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import bootstrap_core
import gcp_base_images
import gcp_catalog
import gcp_deploy
import gcp_deploy_stages
import gcp_install
from gcp_deploy import DeployOptions, import_release_base_images
from gcp_identity_cutover import IdentityCutoverError, _fresh_installation, identity_cutover_boundary
from gcp_inputs import load_deployment_inputs
from gcp_process import GcpCommandError, ProcessResult
from tests.gcp_test_support import PROJECT, FakeRunner, root_config, sample_gcp_outputs, write_bundle
from tests.test_gcp_identity_cutover import chart

__all__ = ["chart"]
DIGEST = "sha256:" + "d" * 64
BASE_IMAGES = {role: f"ghcr.io/acme/bigrae/gce-{role}@{DIGEST}" for role in ("kali", "ubuntu", "dc")}


@pytest.fixture
def inputs(tmp_path: Path):
    return load_deployment_inputs(str(write_bundle(tmp_path / "b")), tmp_path / "w", FakeRunner(), {})


class TestFreshInstallFacts:
    def test_only_an_empty_or_bootstrap_only_installation_is_fresh(self):
        assert _fresh_installation("absent", 0)
        assert not _fresh_installation("absent", 2)
        assert _fresh_installation("policy_pending", 1)
        assert not _fresh_installation("planned", 2)
        assert not _fresh_installation("absent", None)

    def test_boundary_generates_facts_for_an_empty_installation(self, chart, tmp_path, monkeypatch):
        probe = 'SHIFTER_IDENTITY_CUTOVER_RESULT={"state":"absent","users":0}\n'
        runner = FakeRunner().on(["kubectl", "logs"], ProcessResult(0, probe))
        monkeypatch.setattr("gcp_identity_cutover.script_argv", lambda *a: ["true"])
        facts = tmp_path / "cutover.json", tmp_path / "bootstrap.json"
        for path in facts:
            path.write_text("{}")
            path.chmod(0o600)
        generated = []

        def fresh():
            generated.append(True)
            return facts

        with identity_cutover_boundary(runner, chart, tmp_path, fresh_install_facts=fresh) as run:
            assert run.required and run.fresh_install and run.bootstrap_facts == facts[1]
        assert generated == [True]
        retained = 'SHIFTER_IDENTITY_CUTOVER_RESULT={"state":"absent","users":3}\n'
        legacy = FakeRunner().on(["kubectl", "logs"], ProcessResult(0, retained))
        with (
            pytest.raises(IdentityCutoverError, match="requires both private facts"),
            identity_cutover_boundary(legacy, chart, tmp_path, fresh_install_facts=fresh),
        ):
            pass
        assert generated == [True]

    def test_facts_name_the_configured_operator_account(self, inputs, monkeypatch):
        seen = []

        def subject(config, outputs, email):
            seen.append(email)
            return "operator-subject"

        monkeypatch.setattr("gcp_control_plane.gcp_identity_platform_subject", subject)
        cutover, bootstrap = gcp_deploy_stages.fresh_install_facts(inputs, sample_gcp_outputs(PROJECT))
        assert json.loads(bootstrap.read_text()) == {
            "schema": "shifter.identity-bootstrap-facts/v1",
            "issuer": f"https://securetoken.google.com/{PROJECT}",
            "subject": "operator-subject",
        }
        written = json.loads(cutover.read_text())
        assert written["bootstrap_personal_token_eligibility"] is True
        assert written["account_types_by_organization"] == written["owner_user_ids_by_workspace"] == {}
        for path in (cutover, bootstrap):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert seen == ["ops@example.com"]

    def test_subject_lookup_requires_exactly_one_account(self, monkeypatch):
        import gcp_control_plane

        config = gcp_control_plane.GDCBootstrapConfig(project_id=PROJECT, cluster_id="c")
        for response, ok in (({"users": [{"localId": "abc"}]}, True), ({}, False), ({"users": [{}]}, False)):
            monkeypatch.setattr(
                gcp_control_plane, "_gcp_identity_admin_request", lambda _response=response, **kwargs: _response
            )
            if ok:
                assert gcp_control_plane.gcp_identity_platform_subject(config, {}, "Ops@Example.com ") == "abc"
            else:
                with pytest.raises(RuntimeError):
                    gcp_control_plane.gcp_identity_platform_subject(config, {}, "ops@example.com")


class TestCatalog:
    def test_bootstrap_registration_is_a_warning_when_it_fails(self, chart, tmp_path, capsys):
        facts = tmp_path / "bootstrap.json"
        facts.write_text('{"schema": "shifter.identity-bootstrap-facts/v1"}')
        failing = FakeRunner().on(["kubectl", "wait"], ProcessResult(1, "", "timed out"))
        gcp_catalog.initialize_catalog(
            failing, chart, credential_file=None, hostname="portal.example.test", bootstrap_facts=facts
        )
        assert "Automatic catalog registration did not complete" in capsys.readouterr().out
        applied = [
            call for call in failing.find("kubectl", "apply") if "initialize_installation_catalog" in call.input_text
        ]
        assert applied and '"schema": "shifter.identity-bootstrap-facts/v1"' not in "".join(
            part for call in failing.calls for part in call.argv
        )
        assert failing.find("kubectl", "delete", "secret")
        ok = FakeRunner()
        gcp_catalog.initialize_catalog(ok, chart, credential_file=None, hostname="h", bootstrap_facts=facts)
        assert "In-box catalog is registered" in capsys.readouterr().out
        gcp_catalog.initialize_catalog(ok, chart, credential_file=None, hostname="h")
        assert "catalog may be incomplete" in capsys.readouterr().out

    def test_a_supplied_credential_still_fails_the_run(self, chart, tmp_path):
        credential = tmp_path / "token"
        credential.write_text("bearer-proof")
        credential.chmod(0o600)
        failing = FakeRunner().on(["kubectl", "wait"], ProcessResult(1, "", "denied"))
        with pytest.raises(GcpCommandError):
            gcp_catalog.initialize_catalog(failing, chart, credential_file=str(credential), hostname="h")

    def test_catalog_facts_follow_the_cutover_or_the_configured_operator(self, inputs, monkeypatch, capsys):
        class Run:
            bootstrap_facts = Path("/work/identity/bootstrap.json")

        outputs = sample_gcp_outputs(PROJECT)
        assert gcp_deploy_stages._catalog_bootstrap_facts(inputs, outputs, Run(), "token") is None
        assert gcp_deploy_stages._catalog_bootstrap_facts(inputs, outputs, Run(), "") == Run.bootstrap_facts
        monkeypatch.setattr(gcp_deploy_stages, "operator_bootstrap_facts", lambda *a: Path("/w/facts"))
        assert gcp_deploy_stages._catalog_bootstrap_facts(inputs, outputs, object(), "") == Path("/w/facts")

        def unavailable(*args):
            raise RuntimeError("lookup failed")

        monkeypatch.setattr(gcp_deploy_stages, "operator_bootstrap_facts", unavailable)
        assert gcp_deploy_stages._catalog_bootstrap_facts(inputs, outputs, object(), "") is None
        assert "Cannot name the bootstrap administrator" in capsys.readouterr().out


class TestReleaseBaseImages:
    def _manifest(self, base_images):
        from installation.gcp_release import build_release_manifest

        images = {name: f"ghcr.io/acme/bigrae/{name}@{DIGEST}" for name in ("portal", "provisioner", "guacd")}
        images |= {name: f"ghcr.io/acme/bigrae/{name}@{DIGEST}" for name in ("guacamole-client", "openvpn")}
        return build_release_manifest(
            version="1.0.0", source_revision="a" * 40, chart_version="1.0.0", images=images, base_images=base_images
        )

    def test_imports_the_verified_images_only_when_the_bundle_lacks_them(self, tmp_path, monkeypatch):
        from gcp_provenance import ReleaseTrust

        bundle = write_bundle(tmp_path / "b", range_images={})
        inputs = load_deployment_inputs(str(bundle), tmp_path / "w", FakeRunner(), {})
        verified, imported = [], []

        def resolve_selection(base_images, *, repository_id, owner_id, oci, verifier, release_source):
            verified.append((base_images, repository_id, owner_id, release_source))
            return list(base_images)

        def import_selection(artifacts, *, project, region):
            imported.append((artifacts, project, region))
            return {
                role: gcp_base_images.ImportedImage(
                    role, f"shifter-{role}", f"projects/{project}/global/images/{role}", DIGEST, False
                )
                for role in artifacts
            }

        monkeypatch.setattr(gcp_base_images, "resolve_selection", resolve_selection)
        monkeypatch.setattr(gcp_base_images, "import_selection", import_selection)
        monkeypatch.setattr("gcp_provenance.default_verifier", lambda: object())
        written = []
        monkeypatch.setattr(
            "gcp_config_store.write_range_images", lambda store, images: written.append((store, images))
        )
        options = DeployOptions(source="gs://acme-shifter-deployment-config", release_source="mirror.example/r")
        trust = ReleaseTrust(repository_id="11", owner_id="22", source_revision="a" * 40)
        runner = FakeRunner()
        updated = import_release_base_images(runner, options, inputs, self._manifest(BASE_IMAGES), trust)
        # Every guest is verified against the trust anchor before the import.
        assert verified == [(BASE_IMAGES, "11", "22", "mirror.example/r")]
        assert imported == [(["kali", "ubuntu", "dc"], PROJECT, "us-central1")]
        assert set(updated.range_images) == {"linux", "kali", "dc"}
        assert written == [("gs://acme-shifter-deployment-config", dict(updated.range_images))]
        assert import_release_base_images(runner, options, updated, self._manifest(BASE_IMAGES), trust) is updated
        assert import_release_base_images(runner, options, inputs, self._manifest({}), trust) is inputs
        assert len(imported) == 1


class TestCommandLine:
    def test_gcp_install_and_release_resolution_are_wired(self, monkeypatch, capsys):
        import argparse

        import cli
        import gcp_cli

        # The CLI handler sets the process-wide proceed flag; restore it after the test.
        monkeypatch.setitem(bootstrap_core._ASSUME_YES, "enabled", False)

        parser = argparse.ArgumentParser()
        gcp_cli.add_lifecycle_subparsers(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["gcp-install", "--inputs", "/bundle", "--release-manifest", "r.json"])
        seen = []
        monkeypatch.setattr("gcp_install.run_install", lambda options: seen.append(options) or 0)
        assert gcp_cli._HANDLERS["gcp-install"](args) == 0
        assert seen[0] == gcp_install.InstallOptions(inputs="/bundle", release_manifest="r.json")
        assert bootstrap_core.assume_yes_enabled()
        assert "gcp-install" in gcp_cli.LIFECYCLE_COMMANDS

        def newest(registry):
            print("Running: oras repo tags")
            return BASE_IMAGES

        verified = []
        monkeypatch.setattr(cli, "newest_publications", newest)
        monkeypatch.setattr(cli, "resolve_selection", lambda images, **kwargs: verified.append((images, kwargs)))
        monkeypatch.setattr("gcp_provenance.default_verifier", lambda: object())
        assert cli.print_release_base_images("ghcr.io/acme/bigrae", "11", "22") == BASE_IMAGES
        # The release records only guest images that verify against this repository.
        assert verified[0][0] == BASE_IMAGES
        assert (verified[0][1]["repository_id"], verified[0][1]["owner_id"]) == ("11", "22")
        captured = capsys.readouterr()
        # Only the JSON reaches stdout, so the release workflow can parse it.
        assert json.loads(captured.out) == BASE_IMAGES
        assert "Running: oras repo tags" in captured.err


class TestInstall:
    def test_foundation_inputs_come_from_the_bundle_and_the_operator(self, inputs):
        derived = gcp_install.foundation_inputs(inputs, project_number="123", principal="user:ops@example.com")
        assert derived["impersonation_principals"] == {
            purpose: ["user:ops@example.com"] for purpose in ("deploy", "release_scan", "destroy")
        }
        assert derived["deployment_config_bucket_name"] == f"{PROJECT}-deployment-config"
        assert derived["terraform_state_bucket_name"] == f"{PROJECT}-terraform-state"
        assert (
            derived["name_prefix"] == f"shifter-{inputs.environment}" and derived["enable_image_build_network"] is False
        )
        assert "purpose_contexts" not in derived

    def test_operator_principal_kinds(self):
        active = FakeRunner().on(["gcloud", "config"], ProcessResult(0, "ops@example.com\n"))
        assert gcp_install.operator_principal(active, "") == "user:ops@example.com"
        assert gcp_install.operator_principal(FakeRunner(), "ci@p.iam.gserviceaccount.com").startswith(
            "serviceAccount:"
        )
        assert gcp_install.operator_principal(FakeRunner(), "group:ops@example.com") == "group:ops@example.com"
        with pytest.raises(gcp_deploy.GcpDeployError):
            gcp_install.operator_principal(FakeRunner().on(["gcloud", "config"], ProcessResult(0, "")), "")

    def test_operator_password_is_created_once_and_never_on_argv(self, inputs, capsys):
        absent = FakeRunner().on(["gcloud", "secrets", "versions", "describe"], ProcessResult(1))
        assert gcp_install.ensure_operator_password(absent, inputs) is True
        create = absent.find("gcloud", "secrets", "create")[0]
        assert create.input_text and create.input_text not in " ".join(create.argv)
        assert len(create.input_text.split("-")) == 4
        assert "gcloud secrets versions access latest" in capsys.readouterr().out
        assert gcp_install.ensure_operator_password(FakeRunner(), inputs) is False

    def test_install_runs_foundation_secret_publish_then_first_install(self, tmp_path, monkeypatch):
        bundle = write_bundle(tmp_path / "b", config=root_config())
        order = []
        monkeypatch.setattr(gcp_install, "bootstrap_gcp_foundation", lambda path: order.append(("foundation", path)))
        monkeypatch.setattr(gcp_install, "ensure_operator_password", lambda runner, inputs: order.append("password"))
        monkeypatch.setattr(gcp_install, "publish", lambda store, source, runner: order.append(("publish", store)))

        def deploy(options, runner):
            order.append(("deploy", options.source, options.first_install, options.release_manifest))
            return 0

        monkeypatch.setattr(gcp_install, "run_deploy", deploy)
        runner = FakeRunner().on(["gcloud", "config"], ProcessResult(0, "ops@example.com\n"))
        runner.on(["gcloud", "projects", "describe"], ProcessResult(0, "123\n"))
        options = gcp_install.InstallOptions(inputs=str(bundle), release_manifest="release.json")
        assert gcp_install.run_install(options, runner=runner) == 0
        # The library call leaves process-wide prompt state to the CLI.
        assert not bootstrap_core.assume_yes_enabled()
        assert [step if isinstance(step, str) else step[0] for step in order] == [
            "foundation",
            "password",
            "publish",
            "deploy",
        ]
        assert order[-1] == ("deploy", f"gs://{PROJECT}-deployment-config", True, "release.json")
