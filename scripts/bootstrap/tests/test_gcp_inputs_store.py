"""Tests for the GCP deployment input bundle, lock, evidence, and config store (#2571)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gcp_config_store import publish, pull, show, write_range_images
from gcp_evidence import evidence_digest, evidence_prefix, store_evidence, store_verdict
from gcp_inputs import (
    DeploymentInputError,
    gcs_object_url,
    load_deployment_inputs,
    parse_range_images,
    resolve_operator_password,
    validate_environment,
)
from gcp_lock import DeployLockHeldError, acquire_lock, break_lock, lock_url, release_lock
from gcp_process import GcpCommandError, ProcessResult
from tests.gcp_test_support import PROJECT, REVISION, FakeRunner, root_config, write_bundle

STORE = "gs://acme-shifter-deployment-config"


def _gcs_runner(bundle: Path) -> FakeRunner:
    """Serve a local bundle directory as if it were the config bucket."""

    def listing(argv, env, stdin):
        names = sorted(path.name for path in bundle.iterdir() if path.is_file())
        return ProcessResult(0, "\n".join(f"{STORE}/{name}" for name in names) + f"\n{STORE}/locks/\n")

    def copy(argv, env, stdin):
        source, target = argv[-2], argv[-1]
        if source.startswith("gs://"):
            shutil.copyfile(bundle / source.rsplit("/", 1)[-1], target)
        else:
            shutil.copyfile(source, bundle / target.rsplit("/", 1)[-1])
        return ProcessResult(0)

    return FakeRunner().on(["gcloud", "storage", "ls"], listing).on(["gcloud", "storage", "cp"], copy)


class TestDeploymentInputs:
    def test_local_bundle_loads_and_projects_range_images(self, tmp_path: Path):
        bundle = write_bundle(tmp_path / "bundle")
        inputs = load_deployment_inputs(str(bundle), tmp_path / "work", FakeRunner(), {})
        assert inputs.environment == "acme"
        assert inputs.project_id == PROJECT
        assert inputs.region == "us-central1"
        assert inputs.public_hostname == "portal.acme.example"
        assert inputs.profile == "prod"
        assert inputs.range_image_env() == {
            "GCP_RANGE_LINUX_IMAGE": f"projects/{PROJECT}/global/images/shifter-ubuntu-aaaa",
            "GCP_RANGE_KALI_IMAGE": f"projects/{PROJECT}/global/images/shifter-kali-bbbb",
            "GCP_RANGE_DC_IMAGE": f"projects/{PROJECT}/global/images/shifter-dc-cccc",
        }
        assert (tmp_path / "work").stat().st_mode & 0o777 == 0o700
        assert inputs.config_path.stat().st_mode & 0o777 == 0o600
        assert inputs.foundation_inputs_path is None

    def test_gcs_bundle_is_copied_with_the_selected_identity(self, tmp_path: Path):
        bundle = write_bundle(tmp_path / "bucket", extra={"retired-cluster-artifacts.json": "{}"})
        runner = _gcs_runner(bundle)
        env = {"CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "deploy@acme-shifter.iam.gserviceaccount.com"}
        inputs = load_deployment_inputs(STORE, tmp_path / "work", runner, env, environment="acme")
        assert inputs.retired_artifacts_path is not None
        copies = runner.find("gcloud", "storage", "cp")
        assert copies and all(
            call.env["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"].startswith("deploy@") for call in copies
        )

    def test_both_terraform_value_files_in_precedence_order(self, tmp_path: Path):
        """The CI adapter adds terraform.tfvars.json next to the committed HCL values; JSON wins."""
        bundle = write_bundle(tmp_path / "bundle")
        (bundle / "terraform.tfvars").write_text("gke_node_count = 3\n")
        inputs = load_deployment_inputs(str(bundle), tmp_path / "work", FakeRunner(), {}, environment="other")
        assert [path.name for path in inputs.terraform_values_paths] == ["terraform.tfvars", "terraform.tfvars.json"]
        assert inputs.environment == "other"

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda b: (b / "bigrae.yaml").unlink(), "bigrae.yaml"),
            (lambda b: (b / "terraform.tfvars.json").unlink(), "terraform.tfvars or terraform.tfvars.json"),
            (lambda b: (b / "terraform.tfvars.json").write_text("[]"), "JSON object"),
            (lambda b: (b / "range-images.json").write_text('{"linux": "latest"}'), "linux"),
            (lambda b: (b / "range-images.json").write_text('{"android": "x"}'), "unsupported"),
        ],
    )
    def test_invalid_bundles_fail_closed(self, tmp_path: Path, mutate, message):
        bundle = write_bundle(tmp_path / "bundle")
        mutate(bundle)
        with pytest.raises(DeploymentInputError, match=message):
            load_deployment_inputs(str(bundle), tmp_path / "work", FakeRunner(), {})

    def test_rejects_wrong_backend_bad_names_and_missing_directories(self, tmp_path: Path):
        aws = root_config()
        aws["backend"] = "aws"
        bundle = write_bundle(tmp_path / "aws", config=aws)
        with pytest.raises(DeploymentInputError):
            load_deployment_inputs(str(bundle), tmp_path / "w1", FakeRunner(), {})
        with pytest.raises(DeploymentInputError):
            load_deployment_inputs(str(tmp_path / "nowhere"), tmp_path / "w2", FakeRunner(), {})
        with pytest.raises(DeploymentInputError):
            validate_environment("Bad--Name")
        with pytest.raises(DeploymentInputError):
            gcs_object_url("gs://Bad Bucket", "x")
        assert parse_range_images(None) == {}

    def test_operator_password_sources(self, tmp_path: Path):
        inputs = load_deployment_inputs(str(write_bundle(tmp_path / "b")), tmp_path / "w", FakeRunner(), {})
        runner = FakeRunner().on(["gcloud", "secrets"], ProcessResult(0, "s3cret-value\n"))
        assert resolve_operator_password(inputs, runner, {}) == "s3cret-value"
        access = runner.find("gcloud", "secrets", "versions", "access")[0].argv
        assert access[-4:] == ["--secret", "operator-password", "--project", PROJECT]
        assert resolve_operator_password(inputs, FakeRunner(), {}, password_env="PW", process_env={"PW": "x"}) == "x"
        with pytest.raises(DeploymentInputError, match="empty"):
            resolve_operator_password(inputs, FakeRunner(), {}, password_env="PW", process_env={})
        failing = FakeRunner().on(["gcloud", "secrets"], ProcessResult(1, "", "denied"))
        with pytest.raises(DeploymentInputError, match="Secret Manager"):
            resolve_operator_password(inputs, failing, {})
        empty = FakeRunner().on(["gcloud", "secrets"], ProcessResult(0, "\n"))
        with pytest.raises(DeploymentInputError, match="empty latest"):
            resolve_operator_password(inputs, empty, {})

    def test_operator_password_requires_the_settings_block(self, tmp_path: Path):
        config = root_config()
        del config["settings"]["operator_bootstrap"]
        inputs = load_deployment_inputs(
            str(write_bundle(tmp_path / "b", config=config)), tmp_path / "w", FakeRunner(), {}
        )
        with pytest.raises(DeploymentInputError, match="operator_bootstrap is required"):
            resolve_operator_password(inputs, FakeRunner(), {})
        disabled = root_config(operator_bootstrap={"enabled": False})
        inputs = load_deployment_inputs(
            str(write_bundle(tmp_path / "c", config=disabled)), tmp_path / "x", FakeRunner(), {}
        )
        assert resolve_operator_password(inputs, FakeRunner(), {}) == ""
        no_ref = root_config(operator_bootstrap={"admin_email": "ops@example.com"})
        inputs = load_deployment_inputs(
            str(write_bundle(tmp_path / "d", config=no_ref)), tmp_path / "y", FakeRunner(), {}
        )
        # Without a reference, the deploy reads the secret that gcp-install creates (#2679).
        runner = FakeRunner().on(["gcloud", "secrets", "versions", "access"], ProcessResult(0, "generated\n"))
        assert resolve_operator_password(inputs, runner, {}) == "generated"
        access = runner.find("gcloud", "secrets", "versions", "access")[0].argv
        assert access[access.index("--secret") + 1] == f"shifter-{inputs.environment}-operator-password"


class TestDeployLock:
    def test_acquire_and_release_with_generation_preconditions(self, tmp_path: Path):
        runner = FakeRunner().on(["gcloud", "storage", "objects", "describe"], ProcessResult(0, "1712\n"))
        lock = acquire_lock(runner, STORE, {}, command="gcp-deploy", run_id="r1", workdir=tmp_path)
        assert lock.url == f"{STORE}/locks/deploy.lock" == lock_url(STORE)
        assert lock.generation == "1712"
        create = runner.find("gcloud", "storage", "cp")[0]
        assert "--if-generation-match=0" in create.argv
        assert json.loads((tmp_path / "deploy.lock").read_text())["run_id"] == "r1"
        release_lock(runner, lock, {})
        assert "--if-generation-match=1712" in runner.find("gcloud", "storage", "rm")[0].argv

    def test_held_lock_names_the_holder(self, tmp_path: Path):
        holder = json.dumps({"command": "gcp-destroy", "run_id": "r0", "started_at": "2026-10-28T00:00:00Z"})
        runner = (
            FakeRunner()
            .on(["gcloud", "storage", "cp"], ProcessResult(1, "", "HTTPError 412: Precondition Failed"))
            .on(["gcloud", "storage", "cat"], ProcessResult(0, holder))
        )
        with pytest.raises(DeployLockHeldError, match="gcp-destroy run r0"):
            acquire_lock(runner, STORE, {}, command="gcp-deploy", run_id="r1", workdir=tmp_path)

    def test_other_failures_and_break(self, tmp_path: Path):
        runner = FakeRunner().on(["gcloud", "storage", "cp"], ProcessResult(1, "", "permission denied"))
        with pytest.raises(GcpCommandError, match="cannot create"):
            acquire_lock(runner, STORE, {}, command="gcp-deploy", run_id="r1", workdir=tmp_path)
        bad_generation = FakeRunner().on(["gcloud", "storage", "objects"], ProcessResult(0, "none"))
        with pytest.raises(GcpCommandError, match="generation"):
            acquire_lock(bad_generation, STORE, {}, command="gcp-deploy", run_id="r1", workdir=tmp_path)
        assert break_lock(FakeRunner(), STORE, {}) is True
        assert break_lock(FakeRunner().on(["gcloud"], ProcessResult(1)), STORE, {}) is False


class TestEvidence:
    def test_prefix_and_digest_match_the_ci_layout(self, tmp_path: Path):
        assert evidence_prefix("deployments", REVISION, "123", 2) == f"deployments/{REVISION}/123/2"
        for bad in (("other", REVISION, "1", 1), ("deployments", "abc", "1", 1), ("deployments", REVISION, "a/b", 1)):
            with pytest.raises(ValueError):
                evidence_prefix(*bad)
        first = tmp_path / "b.json"
        second = tmp_path / "a.json"
        first.write_text("{}\n")
        second.write_text('{"x": 1}\n')
        # The CI layout is: sha256sum ./*.json | sort | sha256sum
        listing = subprocess.run(
            ["sha256sum", "./a.json", "./b.json"], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout
        expected = subprocess.run(
            ["sha256sum"],
            input="".join(sorted(listing.splitlines(keepends=True))),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        assert evidence_digest([first, second]) == expected

    def test_store_evidence_and_verdict(self, tmp_path: Path):
        report = tmp_path / "running-image-ids.json"
        report.write_text("{}\n")
        runner = FakeRunner()
        prefix = evidence_prefix("deployments", REVISION, "r", 1)
        record = store_evidence(runner, {}, bucket="acme-shifter-release-evidence", prefix=prefix, files=[report])
        assert record.locator == hashlib.sha256(prefix.encode()).hexdigest()
        assert runner.argvs()[0][-1] == f"gs://acme-shifter-release-evidence/{prefix}/running-image-ids.json"
        verdict = store_verdict(
            runner,
            {},
            bucket="acme-shifter-release-evidence",
            record=record,
            source_revision=REVISION,
            result="passed",
            workdir=tmp_path,
        )
        assert json.loads(verdict.read_text())["result"] == "passed"
        with pytest.raises(ValueError):
            store_evidence(runner, {}, bucket="Bad Bucket", prefix=prefix, files=[report])
        with pytest.raises(ValueError):
            store_evidence(runner, {}, bucket="acme-shifter-release-evidence", prefix=prefix, files=[])
        failing = FakeRunner().on(["gcloud"], ProcessResult(1))
        with pytest.raises(GcpCommandError, match="privately"):
            store_evidence(failing, {}, bucket="acme-shifter-release-evidence", prefix=prefix, files=[report])
        with pytest.raises(GcpCommandError, match="verdict"):
            store_verdict(
                failing,
                {},
                bucket="acme-shifter-release-evidence",
                record=record,
                source_revision=REVISION,
                result="passed",
                workdir=tmp_path,
            )


class TestConfigStore:
    def test_publish_pull_show_and_range_images(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        source = write_bundle(tmp_path / "draft")
        bucket = tmp_path / "bucket"
        bucket.mkdir()
        runner = _gcs_runner(bucket)
        uploaded = publish(STORE, str(source), runner=runner)
        assert set(uploaded) == {"bigrae.yaml", "terraform.tfvars.json", "range-images.json"}
        assert sorted(pull(STORE, str(tmp_path / "pulled"), runner=runner)) == sorted(uploaded)
        summary = show(STORE, runner=runner)
        assert summary["environment"] == "acme" and summary["release_trust"] is True
        assert "ops@example.com" not in capsys.readouterr().out
        merged = write_range_images(STORE, {"windows": f"projects/{PROJECT}/global/images/win-1"}, runner=runner)
        assert set(merged) == {"linux", "kali", "dc", "windows"}
        replaced = write_range_images(
            STORE, {"linux": f"projects/{PROJECT}/global/images/family/ubuntu"}, runner=runner, replace=True
        )
        assert replaced == {"linux": f"projects/{PROJECT}/global/images/family/ubuntu"}
        with pytest.raises(DeploymentInputError):
            write_range_images(STORE, {"android": "x"}, runner=runner)
        with pytest.raises(DeploymentInputError):
            write_range_images(STORE, {"linux": "not-an-image"}, runner=runner, replace=True)
        with pytest.raises(DeploymentInputError):
            publish("not-a-bucket", str(source), runner=runner)
