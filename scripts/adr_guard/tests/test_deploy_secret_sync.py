"""Exercise deploy-secret config aliases without contacting GitHub."""

import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/sync-deploy-secrets.sh"


class DeploySecretConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "bigrae.yaml"
        self.config.write_text("version: 1\nbackend: gcp\n", encoding="utf-8")
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\necho 'unexpected GitHub command' >&2\nexit 93\n", encoding="utf-8")
        gh.chmod(0o700)

    def run_script(self, *options, repo=("--repo", "example-org/example-repo")):
        return subprocess.run(
            [
                "bash", str(SCRIPT), "--env", "gcp-dev", "--stack", "config",
                *repo, "--dry-run", *options,
            ],
            cwd=REPO_ROOT,
            env={"PATH": f"{self.root}:/usr/local/bin:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_canonical_and_legacy_options_select_the_same_config(self):
        for option in ("--bigrae-config", "--shifter-config"):
            with self.subTest(option=option):
                result = self.run_script(option, str(self.config))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("would set SHIFTER_CONFIG_GCP_DEV", result.stdout)
                self.assertNotIn("version: 1", result.stdout + result.stderr)

    def test_missing_target_repository_stops(self):
        # The fake gh has no default repository. The script must stop instead of
        # falling back to `gh repo view`, which selects an upstream remote (ADR-069).
        result = self.run_script("--bigrae-config", str(self.config), repo=())
        self.assertEqual(result.returncode, 2)
        self.assertIn("no target repository", result.stderr)

    def test_conflicting_aliases_fail_before_sync(self):
        result = self.run_script(
            "--bigrae-config", str(self.config), "--shifter-config", str(self.root / "other.yaml")
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("conflicting config options", result.stderr)
        self.assertNotIn("would set", result.stdout)

    def test_equal_aliases_are_one_config(self):
        result = self.run_script("--bigrae-config", str(self.config), "--shifter-config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("would set SHIFTER_CONFIG_GCP_DEV"), 1)

    def test_canonical_option_requires_a_value(self):
        result = self.run_script("--bigrae-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("needs a value", result.stderr)


if __name__ == "__main__":
    unittest.main()
