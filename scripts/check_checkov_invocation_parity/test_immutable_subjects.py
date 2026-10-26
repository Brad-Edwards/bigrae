"""Repository grammar extension preserves the pinned security boundary."""

import importlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .compat.immutable_subjects import (
    CHECKOV_VERSION,
    LEGACY_PATTERN,
    repository_pattern,
)


class ImmutableSubjectPatternTests(unittest.TestCase):
    def test_legacy_and_immutable_repositories_are_supported(self):
        pattern = repository_pattern(CHECKOV_VERSION, LEGACY_PATTERN)
        for name in (
            "example/product",
            "example@456/product@123",
            "${var.org}@${var.owner_id}/${var.repo}@${var.repo_id}",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(pattern.fullmatch(name))

    def test_invalid_immutable_identities_are_rejected(self):
        pattern = repository_pattern(CHECKOV_VERSION, LEGACY_PATTERN)
        for name in (
            "*@456/product@123",
            "example@0/product@123",
            "example@456/product@*",
            "example@456/product@123/extra",
        ):
            with self.subTest(name=name):
                self.assertIsNone(pattern.fullmatch(name))

    def test_version_and_parser_drift_fail_closed(self):
        with self.assertRaises(RuntimeError):
            repository_pattern("0.0.0", LEGACY_PATTERN)
        with self.assertRaises(RuntimeError):
            repository_pattern(CHECKOV_VERSION, ".*")


@unittest.skipUnless(
    importlib.util.find_spec("checkov"), "native Checkov container runs these tests"
)
class NativeCheckovTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from .compat.immutable_subjects import activate

        activate()
        cls.policy = importlib.import_module(
            "checkov.terraform.checks.resource.gcp.GithubActionsOIDCTrustPolicy"
        ).check

    def _result(self, subject):
        return self.policy.scan_resource_conf(
            {
                "oidc": [
                    {"issuer_uri": ["https://token.actions.githubusercontent.com"]}
                ],
                "attribute_mapping": [{"google.subject": "assertion.sub"}],
                "attribute_condition": [f"assertion.sub == '{subject}'"],
            }
        ).name

    def test_native_policy_accepts_supported_formats(self):
        for subject in (
            "repo:example/product:environment:deploy",
            "repo:example@456/product@123:environment:deploy",
        ):
            with self.subTest(subject=subject):
                self.assertEqual(self._result(subject), "PASSED")

    def test_native_policy_rejects_unsafe_subjects(self):
        for subject in (
            "repo:*",
            "workflow:build",
            "repo:example@0/product@123",
            "repo:example@456/product@*",
        ):
            with self.subTest(subject=subject):
                self.assertEqual(self._result(subject), "FAILED")

    def test_external_loader_version_drift_stops_the_scanner(self):
        from .compat import immutable_subjects

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin = root / "compat"
            plugin.mkdir()
            (plugin / "__init__.py").touch()
            source = Path(immutable_subjects.__file__).read_text()
            target = plugin / "immutable_subjects.py"
            target.write_text(source)
            (root / "main.tf").write_text(
                'resource "google_iam_workload_identity_pool_provider" "fixture" {\n'
                '  attribute_mapping = { "google.subject" = "assertion.sub" }\n'
                "  attribute_condition = \"assertion.sub == 'repo:example@456/product@123:environment:deploy'\"\n"
                '  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }\n}\n'
            )
            args = [
                sys.executable,
                "-c",
                "import sys; from checkov.main import Checkov; sys.exit(Checkov().run())",
                "--directory",
                str(root),
                "--framework",
                "terraform",
                "--check",
                "CKV_GCP_125",
                "--external-checks-dir",
                str(plugin),
                "--skip-download",
                "--quiet",
            ]
            healthy = subprocess.run(
                args, text=True, capture_output=True, check=False, timeout=30
            )
            self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
            target.write_text(
                source.replace(
                    f'CHECKOV_VERSION = "{CHECKOV_VERSION}"',
                    'CHECKOV_VERSION = "0.0.0"',
                )
            )
            result = subprocess.run(
                args, text=True, capture_output=True, check=False, timeout=30
            )
            self.assertNotEqual(result.returncode, 0)
