"""Tests for the GCP deploy process runner and purpose identities (#2571)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gcp_identity import IdentityError, PurposeIdentities, derived_service_account, validate_service_account
from gcp_process import (
    GcpCommandError,
    ProcessRunner,
    isolated_env,
    merged_env,
    minimal_env,
    scoped_environment,
    script_argv,
)

PY = sys.executable


class TestProcessRunner:
    def test_runs_with_overlay_removal_and_clean_environments(self):
        runner = ProcessRunner({"KEEP": "1", "DROP": "2", "PATH": os.environ.get("PATH", "")})
        script = "import os,sys; print(os.environ.get('KEEP'), os.environ.get('DROP'), os.environ.get('NEW'))"
        result = runner.run([PY, "-c", script], env={"DROP": None, "NEW": "3"})
        assert result.stdout.strip() == "1 None 3"
        clean = runner.run([PY, "-c", script], env=isolated_env(runner.base_env, {"NEW": "4"}))
        assert clean.stdout.strip() == "None None 4"

    def test_failure_reports_a_bounded_tail_unless_sensitive(self):
        runner = ProcessRunner()
        failing = [PY, "-c", "import sys; sys.stderr.write('boom-detail\\n'); sys.exit(3)"]
        with pytest.raises(GcpCommandError, match="boom-detail"):
            runner.run(failing)
        with pytest.raises(GcpCommandError) as caught:
            runner.run(failing, sensitive=True)
        assert "boom-detail" not in str(caught.value)
        assert runner.run(failing, check=False).returncode == 3

    def test_missing_tool_timeout_and_empty_command_fail_clearly(self):
        runner = ProcessRunner()
        with pytest.raises(GcpCommandError, match="not on PATH"):
            runner.run(["definitely-not-a-real-tool-2571"])
        with pytest.raises(GcpCommandError, match="timed out"):
            runner.run([PY, "-c", "import time; time.sleep(5)"], timeout=0.2)
        with pytest.raises(GcpCommandError, match="empty"):
            runner.run([])

    def test_live_environment_and_scopes(self, monkeypatch):
        monkeypatch.setenv("SCOPE_KEY", "outer")
        runner = ProcessRunner()
        with scoped_environment({"SCOPE_KEY": "inner", "SCOPE_NEW": "x"}):
            assert runner.base_env["SCOPE_KEY"] == "inner"
            assert os.environ["SCOPE_NEW"] == "x"
            with scoped_environment({"SCOPE_KEY": None}):
                assert "SCOPE_KEY" not in os.environ
            assert os.environ["SCOPE_KEY"] == "inner"
        assert os.environ["SCOPE_KEY"] == "outer"
        assert "SCOPE_NEW" not in os.environ

    def test_helpers(self, tmp_path: Path):
        assert merged_env({"A": "1", "B": "2"}, {"A": None, "C": "3"}) == {"B": "2", "C": "3"}
        assert minimal_env({"PATH": "/bin", "HOME": "/h", "SECRET": "x"}) == {"PATH": "/bin", "HOME": "/h"}
        assert isolated_env({"PATH": "/bin", "SECRET": "x"}, {"PATH": "/usr/bin"}) == {
            "PATH": "/usr/bin",
            "SECRET": None,
        }
        (tmp_path / "scripts" / "gcp").mkdir(parents=True)
        (tmp_path / "scripts" / "gcp" / "tool.py").write_text("print(1)\n")
        assert script_argv(tmp_path, "tool", "--x") == [sys.executable, str(tmp_path / "scripts/gcp/tool.py"), "--x"]
        with pytest.raises(GcpCommandError, match="missing"):
            script_argv(tmp_path, "absent")


class TestPurposeIdentities:
    def test_derives_the_module_naming_contract(self):
        assert derived_service_account("acme-shifter", "acme", "deploy") == (
            "shifteracme-deploy@acme-shifter.iam.gserviceaccount.com"
        )
        assert derived_service_account("acme-shifter", "acme", "release_scan").startswith("shifteracme-scan@")
        assert derived_service_account("acme-shifter", "x", "destroy", name_prefix="bigrae-x").startswith(
            "bigraex-destroy@"
        )

    def test_rejects_invalid_or_overlong_identities(self):
        with pytest.raises(IdentityError):
            derived_service_account("acme-shifter", "a-very-long-deployment-name", "destroy")
        with pytest.raises(IdentityError):
            derived_service_account("acme-shifter", "acme", "deploy", name_prefix="Bad_Prefix")
        with pytest.raises(IdentityError):
            validate_service_account("user@example.com")

    def test_impersonation_and_ambient_environments(self):
        identities = PurposeIdentities.resolve(
            mode="impersonate",
            project_id="acme-shifter",
            environment="acme",
            deploy="custom-deploy@acme-shifter.iam.gserviceaccount.com",
        )
        assert identities.deploy == "custom-deploy@acme-shifter.iam.gserviceaccount.com"
        assert identities.release_scan == "shifteracme-scan@acme-shifter.iam.gserviceaccount.com"
        env = identities.env("release_scan")
        assert set(env.values()) == {identities.release_scan}
        assert set(env) == {
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
            "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
            "GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT",
        }
        ambient = PurposeIdentities.resolve(mode="ambient", project_id="acme-shifter", environment="acme")
        assert all(value is None for value in ambient.env("deploy").values())
