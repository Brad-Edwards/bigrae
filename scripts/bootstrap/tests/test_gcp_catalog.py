"""Catalog initialization is a separate native, post-activation operation."""

import json
import os
import subprocess
import sys

import pytest
import yaml
from installation.gcp_catalog import render_catalog_job
from installation.gcp_migrations import render_migration_job

from gcp_catalog import initialize_catalog, validate_catalog_credential_path
from gcp_identity_cutover import IdentityCutoverError
from gcp_process import GcpCommandError, ProcessResult
from tests.gcp_test_support import FakeRunner
from tests.test_gcp_identity_cutover import chart

__all__ = ["chart"]


def test_catalog_job_keeps_native_gate_and_exact_runtime_bindings(chart):
    manifest = yaml.safe_dump_all(chart)
    original = next(
        item for item in chart if item.get("kind") == "Deployment" and item["metadata"]["name"] == "portal-web"
    )
    job = render_catalog_job(manifest, job_name="catalog-test", secret_name="catalog-proof")
    pod = job["spec"]["template"]["spec"]
    portal = pod["containers"][0]
    original_pod = original["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == original_pod["serviceAccountName"]
    assert portal["image"] == original_pod["containers"][0]["image"]
    env = {item["name"]: item.get("value") for item in portal["env"]}
    assert env["SHIFTER_IDENTITY_MAINTENANCE"] == "false"
    assert env["SKIP_MIGRATIONS"] == "1"
    assert portal["args"] == [
        "python",
        "manage.py",
        "bootstrap_inbox_catalog",
        "--credential-file",
        "/run/catalog-private/credential",
    ]
    assert "install -m 600" in pod["initContainers"][0]["args"][0]
    assert pod["initContainers"][0]["resources"] == portal["resources"]
    assert pod["initContainers"][1:] == original_pod.get("initContainers", [])
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "catalog-initializer"
    migration = json.dumps(render_migration_job("synthetic-image"))
    assert "bootstrap_inbox_catalog" not in migration
    for name in ("allow-portal-to-openfga", "allow-portal-openfga-egress"):
        policy = next(
            item for item in chart if item.get("kind") == "NetworkPolicy" and item["metadata"]["name"] == name
        )
        assert "catalog-initializer" in json.dumps(policy)


@pytest.mark.parametrize("failed", [False, True])
def test_proof_only_enters_secret_stdin_and_is_cleaned_after_job(chart, tmp_path, failed):
    credential = tmp_path / "credential"
    credential.write_text("synthetic-native-bearer")
    credential.chmod(0o600)
    runner = FakeRunner()
    if failed:
        runner.on(["kubectl", "wait"], ProcessResult(1))
    if failed:
        with pytest.raises(GcpCommandError):
            initialize_catalog(runner, chart, credential_file=credential, hostname="portal.example.test")
    else:
        initialize_catalog(runner, chart, credential_file=credential, hostname="portal.example.test")
    applied = [yaml.safe_load(call.input_text) for call in runner.calls if call.argv[:2] == ["kubectl", "apply"]]
    secret, job = applied
    assert secret["stringData"]["credential"] == "synthetic-native-bearer"
    assert "synthetic-native-bearer" not in json.dumps(job)
    assert "synthetic-native-bearer" not in json.dumps([call.argv for call in runner.calls])
    assert "synthetic-native-bearer" not in json.dumps([call.env for call in runner.calls])
    deletes = [call.argv for call in runner.calls if call.argv[:2] == ["kubectl", "delete"]]
    assert [call[2] for call in deletes] == ["job", "secret"]


def test_missing_proof_does_not_start_a_catalog_job(chart, capsys):
    """#2679: the application is active, so missing proof is a warning with the next step."""
    runner = FakeRunner()
    initialize_catalog(runner, chart, credential_file=None, hostname="portal.example.test")
    assert "Sign in at https://portal.example.test as a native administrator" in capsys.readouterr().out
    assert runner.calls == []


@pytest.mark.parametrize("shape", ["public", "symlink", "directory", "oversize", "empty", "multiline"])
def test_unsafe_proof_file_is_refused_without_blocking(tmp_path, shape):
    path = tmp_path / "credential"
    if shape == "symlink":
        target = tmp_path / "target"
        target.write_text("synthetic")
        target.chmod(0o600)
        path.symlink_to(target)
    elif shape == "directory":
        path.mkdir(mode=0o600)
    else:
        path.write_text({"oversize": "x" * 16_385, "empty": "", "multiline": "first\nsecond"}.get(shape, "synthetic"))
        path.chmod(0o644 if shape == "public" else 0o600)
    with pytest.raises(IdentityCutoverError, match="private 0600 bearer"):
        validate_catalog_credential_path(path)


def test_fifo_refusal_cannot_block_operator_preflight(tmp_path):
    path = tmp_path / "credential"
    os.mkfifo(path, mode=0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from gcp_catalog import validate_catalog_credential_path; "
            "validate_catalog_credential_path(sys.argv[1])",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode != 0
    assert "private 0600 bearer" in result.stderr
