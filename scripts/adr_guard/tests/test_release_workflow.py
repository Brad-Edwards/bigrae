"""Tests for the release-workflow-provenance check (#2571, ADR-070).

Each test writes a small release workflow into a temporary repository, changes
one property, and checks the violation that the check reports. One test runs
the check against the real repository.
"""

from __future__ import annotations

import importlib
import importlib.util
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
RELEASE = importlib.import_module("_guard.checks.release_workflow")
CHECK = RELEASE.check_release_workflow
SHA = "0123456789abcdef0123456789abcdef01234567"

REF_GUARD = """      - name: Reject non-protected dispatch refs
        run: |
          case "${GITHUB_REF}" in
            refs/heads/main) ;;
            *)
              echo "::error::release-gcp must run from refs/heads/main."
              exit 1
              ;;
          esac
"""
CHECKOUT = f"      - uses: actions/checkout@{SHA} # v6.0.3\n"
AUTH = f"""      - name: Login to the release registry
        uses: docker/login-action@{SHA} # v3.7.0
        with:
          registry: ghcr.io
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}
"""
CLOUD_AUTH = f"""      - id: auth
        uses: google-github-actions/auth@{SHA} # v3.0.0
        with:
          workload_identity_provider: ${{{{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}}}
"""
BUILD = f"""      - id: build-portal
        uses: docker/build-push-action@{SHA} # v5.4.0
        with:
          push: true
          provenance: mode=max
          sbom: true
"""
ATTEST = f"""      - uses: actions/attest-build-provenance@{SHA} # v4.1.1
        with:
          subject-name: registry.example/portal
          subject-digest: ${{{{ steps.build-portal.outputs.digest }}}}
          push-to-registry: true
"""


def workflow(
    *,
    on: str = "  workflow_dispatch:\n",
    runs_on: str = "ubuntu-latest",
    environment: str = "    environment: gcp-release\n",
    steps: str | None = None,
) -> str:
    """Return a release workflow that passes the check unless a part is replaced."""
    body = steps if steps is not None else REF_GUARD + CHECKOUT + AUTH + BUILD + ATTEST
    return (
        "name: Release (GCP)\n"
        f"on:\n{on}"
        "permissions: {}\n"
        "jobs:\n"
        "  release:\n"
        f"    runs-on: {runs_on}\n"
        f"{environment}"
        "    permissions:\n"
        "      contents: read\n"
        "      id-token: write\n"
        "      attestations: write\n"
        "    steps:\n"
        f"{body}"
    )


class ReleaseWorkflowProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_check(self, text: str | None, files: list[str] | None = None) -> list[tuple[str, str]]:
        path = self.root / RELEASE.RELEASE_WORKFLOW_PATH
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return [(violation.rule_id, violation.message) for violation in CHECK(self.root, files)]

    def assert_one(self, text: str, rule_id: str, fragment: str) -> None:
        violations = self.run_check(text)
        self.assertEqual(len(violations), 1, violations)
        self.assertEqual(violations[0][0], rule_id)
        self.assertIn(fragment, violations[0][1])

    def test_a_conforming_workflow_passes_and_unrelated_changes_are_skipped(self) -> None:
        self.assertEqual(self.run_check(workflow()), [])
        self.assertEqual(self.run_check(workflow(on="  push:\n"), files=["README.md"]), [])
        self.assertTrue(self.run_check(workflow(on="  push:\n"), files=[RELEASE.RELEASE_WORKFLOW_PATH]))

    def test_only_workflow_dispatch_may_start_the_release(self) -> None:
        for on in ("  workflow_dispatch:\n  push:\n", "  pull_request:\n", "  workflow_dispatch:\n  schedule:\n"):
            with self.subTest(on=on):
                self.assert_one(workflow(on=on), "ADR-004-R23", "only from workflow_dispatch")

    def test_scalar_list_and_empty_triggers_are_read(self) -> None:
        dispatch = "on:\n  workflow_dispatch:\n"
        self.assertEqual(self.run_check(workflow().replace(dispatch, "on: workflow_dispatch\n")), [])
        self.assertEqual(self.run_check(workflow().replace(dispatch, "on: [workflow_dispatch]\n")), [])
        self.assert_one(workflow().replace(dispatch, "on:\n"), "ADR-004-R23", "no event")

    def test_a_build_step_without_inputs_pushes_nothing(self) -> None:
        bare_build = f"      - uses: docker/build-push-action@{SHA} # v5.4.0\n"
        self.assertEqual(self.run_check(workflow(steps=REF_GUARD + CHECKOUT + AUTH + bare_build)), [])
        self.assertEqual(self.run_check(workflow(environment="", steps=CHECKOUT + bare_build)), [])

    def test_a_push_or_cloud_auth_without_a_login_is_still_a_publishing_job(self) -> None:
        for publish in (BUILD + ATTEST, CLOUD_AUTH + BUILD + ATTEST):
            with self.subTest(publish=publish[:40]):
                self.assert_one(workflow(steps=CHECKOUT + publish), "ADR-037-R7", "refs/heads/main")
                self.assert_one(workflow(environment="", steps=REF_GUARD + publish), "ADR-004-R23", "gcp-release")

    def test_the_authenticating_job_binds_the_release_environment_on_a_hosted_runner(self) -> None:
        self.assert_one(workflow(environment=""), "ADR-004-R23", "gcp-release")
        self.assert_one(workflow(environment="    environment: gcp-dev\n"), "ADR-004-R23", "gcp-release")
        self.assertEqual(self.run_check(workflow(environment="    environment:\n      name: gcp-release\n")), [])
        self.assert_one(workflow(runs_on="[self-hosted, gcp-dev]"), "ADR-003-R5", "self-hosted")

    def test_a_release_ref_guard_runs_before_authentication(self) -> None:
        rest = CHECKOUT + AUTH + BUILD + ATTEST
        release_refs = REF_GUARD.replace("refs/heads/main)", "refs/heads/main|refs/heads/dev|refs/heads/deploy-test)")
        self.assertEqual(self.run_check(workflow(steps=release_refs + rest)), [])
        missing = rest
        late = CHECKOUT + AUTH + REF_GUARD + BUILD + ATTEST
        feature = REF_GUARD.replace("refs/heads/main)", "refs/heads/feature|refs/heads/main)") + rest
        wildcard = REF_GUARD.replace("refs/heads/main)", "refs/heads/*)") + rest
        skipped = REF_GUARD.replace("        run: |", "        if: false\n        run: |") + rest
        no_exit = REF_GUARD.replace("exit 1", "true") + rest
        for steps in (missing, late, feature, wildcard, skipped, no_exit):
            with self.subTest(steps=steps[:80]):
                self.assert_one(workflow(steps=steps), "ADR-037-R7", "refs/heads/main")

    def test_every_action_is_pinned_to_a_full_commit_sha(self) -> None:
        steps = REF_GUARD + "      - uses: actions/checkout@v6\n" + AUTH + BUILD + ATTEST
        self.assert_one(workflow(steps=steps), "ADR-037-R1", "actions/checkout@v6")

    def test_every_pushed_image_has_an_sbom_and_a_registry_attestation(self) -> None:
        base = REF_GUARD + CHECKOUT + AUTH
        self.assert_one(workflow(steps=base + BUILD), "ADR-037-R5", "no attestation step signs")
        self.assert_one(
            workflow(steps=base + BUILD + ATTEST.replace("push-to-registry: true", "push-to-registry: false")),
            "ADR-037-R5",
            "push-to-registry: true",
        )
        self.assert_one(
            workflow(steps=base + BUILD.replace("          sbom: true\n", "") + ATTEST),
            "ADR-037-R5",
            "sbom: true",
        )
        unnamed = base + BUILD.replace("      - id: build-portal\n        uses:", "      - uses:") + ATTEST
        self.assert_one(workflow(steps=unnamed), "ADR-037-R5", "unnamed build step")
        local_only = base + BUILD.replace("push: true", "push: false")
        self.assertEqual(self.run_check(workflow(steps=local_only)), [])

    def test_the_deployer_image_is_scanned_before_it_is_attested(self) -> None:
        build = f"""      - id: build-deployer
        uses: docker/build-push-action@{SHA} # v5.4.0
        with:
          file: ./platform/deployer/gcp/Dockerfile
          push: true
          provenance: mode=max
          sbom: true
"""
        scan = """      - name: Scan the deployer image
        env:
          DEPLOYER_DIGEST: ${{ steps.build-deployer.outputs.digest }}
        run: |
          trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \\
            --ignorefile platform/deployer/gcp/trivyignore.yaml \\
            --exit-code 1 "registry.example/deployer@${DEPLOYER_DIGEST}"
"""
        attest = ATTEST.replace("steps.build-portal", "steps.build-deployer")
        base = REF_GUARD + CHECKOUT + AUTH
        self.assertEqual(self.run_check(workflow(steps=base + build + scan + attest)), [])
        for steps in (
            base + build + attest,
            base + build + attest + scan,
            base + build + scan.replace("--exit-code 1", "--exit-code 0") + attest,
            base + build + scan.replace("        env:", "        continue-on-error: true\n        env:") + attest,
            base + build + scan.replace("platform/deployer/gcp/trivyignore.yaml", ".trivyignore.yaml") + attest,
        ):
            with self.subTest(steps=steps[-400:]):
                self.assert_one(workflow(steps=steps), "ADR-070-R1", "scan the deployer image digest")

    def test_a_missing_or_broken_workflow_fails_closed(self) -> None:
        self.assertEqual(self.run_check(None), [("ADR-037-R5", "the GCP release workflow is missing")])
        violations = self.run_check("jobs: [unclosed\n")
        self.assertEqual(len(violations), 1)
        self.assertIn("could not be parsed", violations[0][1])
        self.assert_one("name: x\non:\n  workflow_dispatch:\njobs: {}\n", "ADR-037-R5", "no jobs")

    def test_the_real_release_workflow_passes_and_the_check_is_registered(self) -> None:
        self.assertEqual(CHECK(_REPO_ROOT, None), [])
        self.assertIs(ADR_GUARD.CHECKS["release-workflow-provenance"], CHECK)
        for level in ("fast", "ci", "all"):
            self.assertIn("release-workflow-provenance", ADR_GUARD.CHECK_LEVELS[level])


if __name__ == "__main__":
    unittest.main()
