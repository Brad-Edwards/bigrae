"""Tests for the release-workflow-provenance check (#2571, #2660, ADR-070).

Each test writes a small release workflow and its Release Please caller into a
temporary repository, changes one property, and checks the violation that the
check reports. One test runs the check against the real repository.
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
COMMON = importlib.import_module("_guard.checks._release_workflow_common")
GUESTS = importlib.import_module("_guard.checks._release_workflow_guests")
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


CALL_TRIGGER = """  workflow_call:
    inputs:
      source_revision:
        required: true
        type: string
"""
TRIGGERS = "  workflow_dispatch:\n" + CALL_TRIGGER
CALLER_JOB = """  release-gcp:
    needs: release-please
    if: needs.release-please.outputs.release_commit == 'true'
    permissions:
      contents: read
    uses: ./.github/workflows/release-gcp.yml
    with:
      version: ${{ needs.release-please.outputs.version }}
      source_revision: ${{ needs.release-please.outputs.sha }}
"""


RP_OUTPUTS = """    outputs:
      release_created: ${{ steps.rp.outputs.release_created }}
      release_commit: ${{ steps.release-commit.outputs.release_commit }}
      version: ${{ steps.release-commit.outputs.version }}
      sha: ${{ steps.release-commit.outputs.sha }}
"""
RELEASE_COMMIT_STEP = """      - name: Find the release of this commit
        id: release-commit
        run: echo "release_commit=true" >> "${GITHUB_OUTPUT}"
"""


GUEST_REF_GUARD = """      - name: Reject non-protected dispatch refs
        run: |
          case "${GITHUB_REF}" in
            refs/heads/dev|refs/heads/main) ;;
            *)
              exit 1
              ;;
          esac
"""
GUEST_AUTH = f"""      - id: auth
        uses: google-github-actions/auth@{SHA}
        with:
          workload_identity_provider: ${{{{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}}}
"""
GUEST_PUSH = """      - name: Push the guest image
        id: push
        run: |
          oras push "ghcr.io/${GITHUB_REPOSITORY,,}/gce-kali:gce-1" "kali.tar.gz:application/octet-stream"
"""
GUEST_ATTEST = f"""      - uses: actions/attest-build-provenance@{SHA} # v4.1.1
        with:
          subject-name: ${{{{ steps.push.outputs.package }}}}
          subject-digest: ${{{{ steps.push.outputs.digest }}}}
          push-to-registry: true
"""
GUEST_VALIDATE_JOB = """  validate:
    needs: build
    uses: ./.github/workflows/packer-gcp-validate.yml
    with:
      source_image: ${{ needs.build.outputs.image_name }}
"""


def guest_build(
    *,
    inputs: str = "",
    validate_job: str = GUEST_VALIDATE_JOB,
    needs: str = "[build, validate]",
    publish_environment: str = "gcp-image-build",
    publish_steps: str = GUEST_REF_GUARD + GUEST_AUTH + GUEST_PUSH + GUEST_ATTEST,
) -> str:
    """Return a guest build workflow that passes the check unless a part is replaced."""
    return (
        "name: Packer GCE Image Build\n"
        "on:\n  workflow_dispatch:\n    inputs:\n      image_type:\n        type: string\n"
        f"{inputs}"
        "jobs:\n"
        "  build:\n    runs-on: ubuntu-latest\n    environment: gcp-image-build\n    steps:\n"
        f"{GUEST_REF_GUARD}{GUEST_AUTH}"
        f"{validate_job}"
        f"  publish:\n    needs: {needs}\n    runs-on: ubuntu-latest\n"
        f"    environment: {publish_environment}\n    steps:\n"
        f"{publish_steps}"
    )


def guest_validate(*, on: str = "  workflow_call:\n", environment: str = "gcp-image-validate") -> str:
    """Return a validation workflow that passes the check unless a part is replaced."""
    return (
        "name: Packer GCE Image Validate\n"
        f"on:\n{on}"
        "jobs:\n"
        f"  validate:\n    runs-on: ubuntu-latest\n    environment: {environment}\n    steps:\n"
        f"{GUEST_REF_GUARD}{GUEST_AUTH}"
    )


def release_please(
    *,
    on: str = "  push:\n    branches: [main]\n",
    caller: str = CALLER_JOB,
    outputs: str = RP_OUTPUTS,
    step: str = RELEASE_COMMIT_STEP,
) -> str:
    """Return a Release Please workflow that passes the check unless a part is replaced."""
    return (
        "name: Release Please\n"
        f"on:\n{on}"
        "jobs:\n"
        "  release-please:\n"
        "    runs-on: ubuntu-latest\n"
        f"{outputs}"
        "    steps:\n"
        f"      - uses: googleapis/release-please-action@{SHA} # v5.0.0\n"
        "        id: rp\n"
        f"{step}"
        f"{caller}"
    )


def workflow(
    *,
    on: str = TRIGGERS,
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

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_check(
        self,
        text: str | None,
        files: list[str] | None = None,
        *,
        caller: str | None = None,
    ) -> list[tuple[str, str]]:
        if text is not None:
            self.write(RELEASE.RELEASE_WORKFLOW_PATH, text)
        self.write(COMMON.RELEASE_PLEASE_WORKFLOW_PATH, caller if caller is not None else release_please())
        if not (self.root / GUESTS.GUEST_WORKFLOW_PATH).exists():
            self.write(GUESTS.GUEST_WORKFLOW_PATH, guest_build())
            self.write(GUESTS.VALIDATE_WORKFLOW_PATH, guest_validate())
        return [(violation.rule_id, violation.message) for violation in CHECK(self.root, files)]

    def assert_one(self, text: str, rule_id: str, fragment: str, *, caller: str | None = None) -> None:
        violations = self.run_check(text, caller=caller)
        self.assertEqual(len(violations), 1, violations)
        self.assertEqual(violations[0][0], rule_id)
        self.assertIn(fragment, violations[0][1])

    def test_a_conforming_workflow_passes_and_unrelated_changes_are_skipped(self) -> None:
        self.assertEqual(self.run_check(workflow()), [])
        self.assertEqual(self.run_check(workflow(on="  push:\n"), files=["README.md"]), [])
        self.assertTrue(self.run_check(workflow(on="  push:\n"), files=[RELEASE.RELEASE_WORKFLOW_PATH]))
        # Any workflow file can add a caller, so a change to one runs the check.
        self.assertTrue(self.run_check(workflow(on="  push:\n"), files=[".github/workflows/other.yml"]))

    def test_only_dispatch_and_call_may_start_the_release(self) -> None:
        for on in (
            "  workflow_dispatch:\n",
            CALL_TRIGGER,
            TRIGGERS + "  push:\n",
            "  pull_request:\n",
            TRIGGERS + "  release:\n",
        ):
            with self.subTest(on=on):
                self.assert_one(workflow(on=on), "ADR-004-R23", "only from workflow_dispatch and workflow_call")

    def test_the_call_trigger_requires_the_source_revision(self) -> None:
        for on in (
            "  workflow_dispatch:\n  workflow_call:\n",
            TRIGGERS.replace("required: true", "required: false"),
            TRIGGERS.replace("source_revision", "revision"),
        ):
            with self.subTest(on=on):
                self.assert_one(workflow(on=on), "ADR-004-R23", "require the source_revision input")

    def test_scalar_list_and_empty_triggers_are_read(self) -> None:
        triggers = f"on:\n{TRIGGERS}"
        self.assert_one(workflow().replace(triggers, "on: workflow_dispatch\n"), "ADR-004-R23", "workflow_call")
        self.assert_one(
            workflow().replace(triggers, "on: [workflow_dispatch, workflow_call]\n"),
            "ADR-004-R23",
            "source_revision",
        )
        self.assert_one(workflow().replace(triggers, "on:\n"), "ADR-004-R23", "no event")

    def test_only_one_gated_release_please_job_calls_the_release(self) -> None:
        self.write(".github/workflows/other.yml", "on: push\njobs:\n" + CALLER_JOB.replace("release-gcp:", "rogue:"))
        violations = self.run_check(workflow())
        self.assertEqual(
            violations, [("ADR-004-R23", "job 'rogue' calls the release workflow; only release-please.yml may call it")]
        )
        (self.root / ".github/workflows/other.yml").write_text("jobs: [unclosed\n", encoding="utf-8")
        self.assertEqual(self.run_check(workflow()), [])
        for caller in (
            release_please(caller=""),
            release_please(caller=CALLER_JOB + CALLER_JOB.replace("release-gcp:", "again:")),
        ):
            with self.subTest(caller=caller[-120:]):
                self.assert_one(workflow(), "ADR-004-R23", "exactly one job", caller=caller)

    def test_release_outputs_come_from_the_release_commit_step(self) -> None:
        for outputs, fragment in (
            (RP_OUTPUTS.replace("steps.release-commit.outputs.sha", "steps.rp.outputs.sha"), "output sha"),
            (RP_OUTPUTS.replace("      version: ${{ steps.release-commit.outputs.version }}\n", ""), "output version"),
            (
                RP_OUTPUTS.replace("steps.release-commit.outputs.release_commit", "steps.rp.outputs.release_created"),
                "output release_commit",
            ),
        ):
            with self.subTest(fragment=fragment):
                self.assert_one(workflow(), "ADR-004-R23", fragment, caller=release_please(outputs=outputs))
        conditional = RELEASE_COMMIT_STEP.replace("        run:", "        if: false\n        run:")
        ignored = RELEASE_COMMIT_STEP.replace("        run:", "        continue-on-error: true\n        run:")
        for step in ("", conditional, ignored):
            with self.subTest(step=step):
                self.assert_one(
                    workflow(), "ADR-004-R23", "unconditional 'release-commit' step", caller=release_please(step=step)
                )
        before_action = release_please(step="").replace("    steps:\n", "    steps:\n" + RELEASE_COMMIT_STEP)
        self.assert_one(workflow(), "ADR-004-R23", "after the Release Please action", caller=before_action)
        no_action = release_please().replace("googleapis/release-please-action", "example/other-action")
        self.assert_one(workflow(), "ADR-004-R23", "no Release Please action step", caller=no_action)

    def test_release_please_starts_only_on_a_push_to_main(self) -> None:
        for on in (
            "  push:\n    branches: [main, dev]\n",
            "  push:\n    branches: [main]\n  workflow_dispatch:\n",
            "  push:\n    branches: [main]\n    paths: [version.txt]\n",
            "  push:\n",
        ):
            with self.subTest(on=on):
                self.assert_one(workflow(), "ADR-004-R23", "only on a push to main", caller=release_please(on=on))

    def test_the_caller_is_gated_passes_the_tagged_commit_and_inherits_no_secrets(self) -> None:
        cases = (
            (CALLER_JOB.replace("release-gcp.yml\n", "release-gcp.yml@main\n"), "with no ref"),
            (CALLER_JOB.replace("    needs: release-please\n", ""), "release_commit == 'true'"),
            (CALLER_JOB.replace("== 'true'", "!= 'true'"), "release_commit == 'true'"),
            (CALLER_JOB.replace("outputs.release_commit", "outputs.release_created"), "release_commit == 'true'"),
            (
                CALLER_JOB.replace("    if: needs.release-please.outputs.release_commit == 'true'\n", ""),
                "release_commit",
            ),
            (CALLER_JOB.replace("outputs.sha", "outputs.tag_name"), "pass source_revision"),
            (
                CALLER_JOB.replace("      source_revision: ${{ needs.release-please.outputs.sha }}\n", ""),
                "pass source_revision",
            ),
            (CALLER_JOB + "    secrets: inherit\n", "no secrets"),
            (CALLER_JOB.replace("outputs.version", "outputs.tag_name"), "pass version"),
        )
        for job, fragment in cases:
            with self.subTest(fragment=fragment, job=job[-160:]):
                self.assert_one(workflow(), "ADR-004-R23", fragment, caller=release_please(caller=job))

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
        self.assert_one(f"name: x\non:\n{TRIGGERS}jobs: {{}}\n", "ADR-037-R5", "no jobs")

    def test_the_real_release_workflow_passes_and_the_check_is_registered(self) -> None:
        self.assertEqual(CHECK(_REPO_ROOT, None), [])
        self.assertIs(ADR_GUARD.CHECKS["release-workflow-provenance"], CHECK)
        for level in ("fast", "ci", "all"):
            self.assertIn("release-workflow-provenance", ADR_GUARD.CHECK_LEVELS[level])


class GuestPublicationTests(unittest.TestCase):
    """#2668: a guest image publishes only after validation of the exact candidate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def violations(self, build: str | None = None, validate: str | None = None) -> list[tuple[str, str, str]]:
        for path, text in (
            (GUESTS.GUEST_WORKFLOW_PATH, guest_build() if build is None else build),
            (GUESTS.VALIDATE_WORKFLOW_PATH, guest_validate() if validate is None else validate),
        ):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return [(v.rule_id, v.path, v.message) for v in GUESTS._guest_publication_violations(self.root)]

    def assert_one(self, rule_id: str, fragment: str, **workflows: str) -> None:
        found = self.violations(**workflows)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], rule_id)
        self.assertIn(fragment, found[0][2])

    def test_conforming_guest_workflows_pass(self) -> None:
        self.assertEqual(self.violations(), [])

    def test_the_build_selects_no_tenant(self) -> None:
        for name in ("environment", "publish_target"):
            with self.subTest(name=name):
                self.assert_one(
                    "ADR-037-R9",
                    f"remove the '{name}' input",
                    build=guest_build(inputs=f"      {name}:\n        type: string\n"),
                )

    def test_publication_needs_the_validation_job(self) -> None:
        self.assert_one("ADR-037-R9", "without needing the validation job", build=guest_build(needs="[build]"))
        self.assert_one(
            "ADR-037-R9", "without needing the validation job", build=guest_build(validate_job="", needs="[build]")
        )

    def test_validation_is_called_locally_without_secrets(self) -> None:
        remote = GUEST_VALIDATE_JOB.replace(
            "./.github/workflows/packer-gcp-validate.yml", "./.github/workflows/packer-gcp-validate.yml@main"
        )
        found = self.violations(build=guest_build(validate_job=remote))
        self.assertIn("ADR-004-R23", {rule for rule, _, _ in found})
        self.assert_one(
            "ADR-004-R23",
            "pass no secrets",
            build=guest_build(validate_job=GUEST_VALIDATE_JOB + "    secrets: inherit\n"),
        )

    def test_the_pushed_digest_is_attested_to_the_registry(self) -> None:
        unsigned = GUEST_REF_GUARD + GUEST_AUTH + GUEST_PUSH
        self.assert_one("ADR-037-R5", "no attestation step signs", build=guest_build(publish_steps=unsigned))
        local_only = GUEST_ATTEST.replace("push-to-registry: true", "push-to-registry: false")
        self.assert_one(
            "ADR-037-R5", "no attestation step signs", build=guest_build(publish_steps=unsigned + local_only)
        )

    def test_publishing_jobs_bind_their_environment_and_reject_other_refs_first(self) -> None:
        self.assert_one(
            "ADR-004-R23", "'gcp-image-build' Environment", build=guest_build(publish_environment="gcp-build-dev")
        )
        late = GUEST_AUTH + GUEST_REF_GUARD + GUEST_PUSH + GUEST_ATTEST
        self.assert_one("ADR-037-R7", "rejects every ref", build=guest_build(publish_steps=late))
        wide = GUEST_REF_GUARD.replace("refs/heads/dev|refs/heads/main", "refs/heads/dev|refs/heads/deploy-test")
        self.assert_one(
            "ADR-037-R7",
            "rejects every ref",
            build=guest_build(publish_steps=wide + GUEST_AUTH + GUEST_PUSH + GUEST_ATTEST),
        )

    def test_validation_is_reusable_only_and_binds_its_environment(self) -> None:
        self.assert_one("ADR-004-R23", "only from workflow_call", validate=guest_validate(on="  workflow_dispatch:\n"))
        self.assert_one(
            "ADR-004-R23", "'gcp-image-validate' Environment", validate=guest_validate(environment="gcp-validate-dev")
        )

    def test_missing_workflows_fail_closed(self) -> None:
        self.assertTrue(GUESTS._guest_publication_violations(self.root))

    def test_the_real_guest_workflows_pass(self) -> None:
        self.assertEqual(GUESTS._guest_publication_violations(_REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
