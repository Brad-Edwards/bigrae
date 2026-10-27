"""Tests for the ADR-069 repository-identity-literals check.

Each test builds a small synthetic repository with its own identity
declaration, so no real repository identity appears in the test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_adr_guard():
    spec = importlib.util.spec_from_file_location("adr_guard", _REPO_ROOT / "scripts" / "adr_guard" / "adr_guard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["adr_guard"] = module
    spec.loader.exec_module(module)
    return module


ADR_GUARD = _load_adr_guard()
CHECK = ADR_GUARD.repository_identity.check_repository_identity_literals

IDENTITY = {
    "repository": "example-org/example-repo",
    "repository_id": "123456789",
    "owner": "example-org",
    "owner_id": "12345678",
    "former_repositories": ["example-org/old-repo"],
}


class RepositoryIdentityLiteralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write("docs/adr/repository-identity.json", json.dumps(IDENTITY))
        self.write(".ground-control.yaml", "schema_version: 1\ngithub_repo: example-org/example-repo\n")

    def write(self, rel: str, text: str) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return rel

    def run_check(self, *files: str) -> list:
        return CHECK(self.root, list(files))

    def test_literals_in_code_fail(self) -> None:
        files = [
            self.write(".github/workflows/deploy.yml", "run: gh attestation verify --repo example-org/example-repo\n"),
            self.write("scripts/tool.sh", 'REPO="Example-Org/Example-Repo"\n'),
            self.write("platform/terraform/main.tf", 'default = "example-org/old-repo"\n'),
            self.write("tests/test_x.py", 'ID = "123456789"\n'),
            self.write("app/config.py", 'IMAGE = "ghcr.io/example-org/image"\n'),
        ]
        violations = self.run_check(*files)
        self.assertEqual(sorted({v.path for v in violations}), sorted(files))
        messages = " ".join(v.message for v in violations)
        for kind in ("repository name", "former repository name", "repository ID", "registry owner namespace"):
            self.assertIn(kind, messages)
        self.assertTrue(all(v.rule_id == "ADR-069-R6" for v in violations))

    def test_upstream_guard_ids_in_workflows_pass(self) -> None:
        rel = self.write(
            ".github/workflows/_quality.yml",
            "if: ${{ github.repository_id == '123456789' && github.repository_owner_id == '12345678' }}\n",
        )
        self.assertEqual(self.run_check(rel), [])

    def test_guard_ids_outside_workflows_fail(self) -> None:
        rel = self.write("scripts/gate.py", "github.repository_id == '123456789'\n")
        self.assertEqual(len(self.run_check(rel)), 1)

    def test_allowed_references_pass(self) -> None:
        files = [
            self.write("docs/runbook.yaml", "url: https://github.com/example-org/example-repo/issues/1\n"),
            self.write("README.md", "Home: https://github.com/example-org/example-repo\n"),
            self.write("mkdocs.yml", "repo_name: example-org/example-repo\n"),
            self.write("platform/terraform/global/iam/dev.tfvars", 'github_repository_id = "123456789"\n'),
            self.write(
                "shifter/environment-packs/demo/associated-artifacts.json",
                '{"source": "example-org/old-repo"}\n',
            ),
        ]
        self.assertEqual(self.run_check(*files), [])

    def test_similar_names_do_not_match(self) -> None:
        rel = self.write("app/names.py", 'A = "example-org/example-repo-tools"\nB = "1234567890"\n')
        self.assertEqual(self.run_check(rel), [])

    def test_ground_control_must_match_the_declaration(self) -> None:
        self.write(".ground-control.yaml", "github_repo: example-org/other-repo\n")
        violations = self.run_check()
        self.assertEqual([v.path for v in violations], [".ground-control.yaml"])

    def test_malformed_declaration_fails_closed(self) -> None:
        self.write("docs/adr/repository-identity.json", json.dumps({**IDENTITY, "repository_id": "abc"}))
        violations = self.run_check()
        self.assertEqual([v.path for v in violations], ["docs/adr/repository-identity.json"])

    def test_real_repository_passes(self) -> None:
        self.assertEqual(CHECK(_REPO_ROOT, None), [])


if __name__ == "__main__":
    unittest.main()
