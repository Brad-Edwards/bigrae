"""The clean-checkout ``make`` lanes run the same commands as their CI jobs (#2090).

``docs/dev/testing.md`` publishes one command matrix. Its rows for the React SPA
and the MCP packages name ``make test-spa`` and ``make test-mcp`` as the local
form of the ``shifter-platform-spa``, ``mcp-lint`` and ``mcp-tests`` jobs in
``.github/workflows/_quality.yml``. ``make test`` includes both lanes.

The recipes are read through ``make --dry-run`` and compared with the commands
the workflow runs, so a CI step that the local lane omits fails here. The MCP
lane must cover every package that has a ``package.json``, so a new package
cannot drop out of the local lane. Nothing here installs packages or runs them.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "adr_guard.py"
SPEC = importlib.util.spec_from_file_location("adr_guard", MODULE_PATH)
ADR_GUARD = importlib.util.module_from_spec(SPEC)
sys.modules["adr_guard"] = ADR_GUARD
SPEC.loader.exec_module(ADR_GUARD)

REPO_ROOT = Path(__file__).resolve().parents[3]
QUALITY_WORKFLOW = ".github/workflows/_quality.yml"
SPA_DIR = "shifter/shifter_platform/frontend"


def _dry_run(target: str) -> str:
    """Commands ``make <target>`` would run, with line continuations folded."""
    env = {key: value for key, value in os.environ.items() if key != "MAKEFLAGS"}
    result = subprocess.run(
        ["make", "--no-print-directory", "--dry-run", target],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(f"make --dry-run {target} failed: {result.stderr}")
    return " ".join(result.stdout.replace("\\\n", " ").split())


def _job_commands(job: str, working_directory: str | None = None) -> list[str]:
    """Normalized ``run`` commands of a quality job, optionally for one directory."""
    workflow = ADR_GUARD._dw_load_workflow(REPO_ROOT, QUALITY_WORKFLOW)
    commands = []
    for step in workflow["jobs"][job]["steps"]:
        if "run" not in step:
            continue
        if working_directory is not None and step.get("working-directory") != working_directory:
            continue
        commands.append(" ".join(step["run"].split()))
    return commands


def _mcp_packages() -> list[str]:
    return sorted(path.parent.name for path in (REPO_ROOT / "mcp").glob("*/package.json"))


class SpaLaneTests(unittest.TestCase):
    """``make test-spa`` runs every command of the ``shifter-platform-spa`` job."""

    def test_runs_the_ci_commands_in_order_from_the_spa_package(self) -> None:
        recipe = _dry_run("test-spa")
        self.assertIn(f"cd {SPA_DIR}", recipe)
        ci_commands = _job_commands("shifter-platform-spa", SPA_DIR)
        self.assertGreaterEqual(len(ci_commands), 4, "the SPA job lost its package commands")
        positions = [recipe.find(command) for command in ci_commands]
        self.assertNotIn(-1, positions, f"make test-spa omits a CI command: {ci_commands}")
        self.assertEqual(positions, sorted(positions), "make test-spa reorders the CI commands")


class McpLaneTests(unittest.TestCase):
    """``make test-mcp`` lints and tests every MCP package like CI does."""

    def test_covers_every_package(self) -> None:
        recipe = _dry_run("test-mcp")
        packages = _mcp_packages()
        self.assertTrue(packages, "no MCP packages found")
        for package in packages:
            self.assertRegex(recipe, rf"for pkg in [^;]*\b{package}\b")

    def test_runs_the_lint_and_test_job_commands(self) -> None:
        recipe = _dry_run("test-mcp")
        for job in ("mcp-lint", "mcp-tests"):
            for command in _job_commands(job):
                self.assertIn(command, recipe, f"make test-mcp omits the {job} command {command!r}")

    def test_a_failing_package_fails_the_lane(self) -> None:
        recipe = _dry_run("test-mcp")
        self.assertTrue(recipe.startswith("set -e;"), "a failing package must stop the loop")


class PackageLaneParityTests(unittest.TestCase):
    """Package lanes do not drop a CI check that their package configures."""

    def test_packer_lane_enforces_the_package_coverage_floor(self) -> None:
        recipe = _dry_run("test-packer")
        ci_pytest = next(c for c in _job_commands("packer-tests") if "pytest" in c)
        self.assertIn("--cov", ci_pytest)
        self.assertRegex(recipe, r"--with pytest-cov pytest tests/ --cov\b")

    def test_postgres_lane_collects_static_files_like_ci(self) -> None:
        recipe = _dry_run("test-platform-postgres")
        ci_commands = " ".join(_job_commands("shifter-platform-tests-postgres", "shifter/shifter_platform"))
        self.assertIn("manage.py collectstatic --noinput", ci_commands)
        collect = recipe.find("manage.py collectstatic --noinput")
        self.assertGreaterEqual(collect, 0, "make test-platform-postgres omits collectstatic")
        self.assertLess(collect, recipe.find("pytest tests/"))
        self.assertIn("TEST_DB_BACKEND=postgres", recipe[:collect])


class AggregateLaneTests(unittest.TestCase):
    """``make test`` includes the SPA and MCP lanes."""

    def test_make_test_runs_the_spa_and_mcp_lanes(self) -> None:
        aggregate = _dry_run("test")
        self.assertIn(_dry_run("test-spa"), aggregate)
        self.assertIn(_dry_run("test-mcp"), aggregate)


if __name__ == "__main__":
    unittest.main()
