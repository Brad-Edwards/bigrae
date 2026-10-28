"""Invariants for the ``dev`` to ``main`` promotion path (#1868).

Two artifacts have to agree for a promotion to work: the root Makefile's
``devmain`` target, which opens the PR, and ``pr-title-lint.yml``, whose
``Lint PR title`` context is a required status check on ``main``.

``devmain`` passes an explicit ``--repo``. A clone can carry an upstream
remote. Then ``gh pr create`` resolves its base repository to the fork parent,
and an unpinned invocation opens the promotion PR against the upstream project.
ADR-069 rule 4 makes the clone's ``gh`` default repository
(``gh repo set-default --view``) the target. The recipe passes that value as
``--repo`` and never relies on the implicit base of ``gh``. When no default is
set, make stops before any command runs. The explicit pin is the load-bearing
property of the recipe rather than a stylistic preference.

The suite puts a fake ``gh`` first on ``PATH``. It answers only
``repo set-default --view``, with a synthetic repository, and fails on any other
call. The checks therefore do not depend on the configuration of the local
clone, and the recipe holds no repository literal.

The title lint has to trigger on ``main``. #1776 narrowed the workflow to PRs
targeting ``dev`` while ``Lint PR title`` stayed a required context on ``main``,
which leaves the context permanently unreported on a promotion PR and blocks
the merge behind an admin bypass. The suite holds the trigger and the title
together: the title ``devmain`` sends must satisfy the grammar the workflow
enforces on the branch it targets.

The recipe is read through ``make --dry-run`` so the assertions run against
make's own expansion (variables included) rather than against Makefile text,
and ``shlex`` turns the expansion into argv so the checks are on parsed flags
rather than substrings. A recipe rewritten to reach the same argv a different
way still passes; one that drops ``--repo`` or pins a literal fails. Nothing
here contacts GitHub or creates a pull request.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "adr_guard.py"
SPEC = importlib.util.spec_from_file_location("adr_guard", MODULE_PATH)
ADR_GUARD = importlib.util.module_from_spec(SPEC)
sys.modules["adr_guard"] = ADR_GUARD
SPEC.loader.exec_module(ADR_GUARD)

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"

# Synthetic gh default repositories (ADR-069: tests use synthetic identities).
# Two values show that --repo follows the gh default rather than a literal.
SYNTHETIC_REPOS = ("example-org/example-repo", "example-org/other-repo")
OVERRIDE_REPO = "example-org/override-repo"

# Answers only `gh repo set-default --view`. An unset default mirrors real gh:
# nothing on stdout, a message on stderr, exit 0.
FAKE_GH = """#!/bin/sh
if [ "$#" -eq 3 ] && [ "$1" = repo ] && [ "$2" = set-default ] && [ "$3" = --view ]; then
  if [ -n "$FAKE_GH_DEFAULT" ]; then
    printf '%s\\n' "$FAKE_GH_DEFAULT"
  else
    echo "no default repository has been set" >&2
  fi
  exit 0
fi
echo "fake gh: unexpected call: $*" >&2
exit 97
"""

PROMOTION_TITLE = "chore(main): promote dev"
# release-please's PR title shape (docs/DEVELOPMENT_WORKFLOW.md), which linting
# `main` newly brings into scope.
RELEASE_PR_TITLE = "chore(main): release 3.104.0"

# Branches whose PRs must have the `Lint PR title` context reported, because
# branch protection requires that context on both.
TITLE_LINT_BRANCHES = ("dev", "main")

# Targets that must never reach the promotion command: running a verification
# lane must not create a pull request.
VERIFICATION_TARGETS = ("test", "policy")


def _run_make(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run make in the repo root and return the completed process.

    ``--no-print-directory`` because this suite itself runs from a ``make``
    lane: a sub-make would otherwise interleave its own ``Entering directory``
    bookkeeping with the recipe under inspection.
    """
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _make(*args: str, env: dict[str, str] | None = None) -> str:
    """Run make in the repo root, require success, and return stdout."""
    result = _run_make(*args, env=env)
    if result.returncode != 0:
        raise AssertionError(f"make {' '.join(args)} failed: {result.stderr}")
    return result.stdout


def _fake_gh_env(bin_dir: str, default_repo: str) -> dict[str, str]:
    """Environment with the fake gh first on PATH and a given gh default.

    DEVMAIN_REPO and MAKEFLAGS are removed so an override in the caller's
    environment or parent make cannot reach the recipe under test.
    """
    env = {key: value for key, value in os.environ.items() if key not in ("DEVMAIN_REPO", "MAKEFLAGS")}
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_GH_DEFAULT"] = default_repo
    return env


def _dry_run_argv(target: str, env: dict[str, str] | None = None, *overrides: str) -> list[str]:
    """Parsed argv of the commands ``make <target>`` would run.

    Shell line continuations are folded the way a shell folds them before
    tokenizing, so a recipe wrapped across lines parses identically to a
    single-line one.
    """
    expanded = _make("--dry-run", target, *overrides, env=env).replace("\\\n", " ")
    return shlex.split(expanded)


def _flag(argv: list[str], name: str) -> str | None:
    """Value of ``--name <value>`` or ``--name=<value>``; None when absent."""
    for index, token in enumerate(argv):
        if token == name:
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return None


def _phony_targets() -> set[str]:
    """Targets declared .PHONY, following backslash continuations."""
    targets: set[str] = set()
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(".PHONY:"):
            body = line[len(".PHONY:") :]
            while body.rstrip().endswith("\\") and index + 1 < len(lines):
                body = body.rstrip()[:-1]
                index += 1
                body += lines[index]
            targets.update(body.split())
        index += 1
    return targets


class DevmainTargetTests(unittest.TestCase):
    """The promotion command make would actually run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bin_dir = tempfile.TemporaryDirectory()
        fake_gh = Path(cls.bin_dir.name) / "gh"
        fake_gh.write_text(FAKE_GH, encoding="utf-8")
        fake_gh.chmod(0o755)
        cls.env = _fake_gh_env(cls.bin_dir.name, SYNTHETIC_REPOS[0])
        cls.recipe = _make("--dry-run", "devmain", env=cls.env)
        cls.argv = _dry_run_argv("devmain", cls.env)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bin_dir.cleanup()

    def test_invokes_gh_pr_create(self) -> None:
        self.assertEqual(self.argv[:3], ["gh", "pr", "create"])

    def test_passes_the_gh_default_repository(self) -> None:
        """Without --repo, gh targets the fork parent of the clone.

        --repo follows each synthetic gh default, so no repository literal
        decides the target.
        """
        for repo in SYNTHETIC_REPOS:
            with self.subTest(repo=repo):
                argv = _dry_run_argv("devmain", _fake_gh_env(self.bin_dir.name, repo))
                self.assertEqual(_flag(argv, "--repo"), repo)

    def test_explicit_override_replaces_the_gh_default(self) -> None:
        argv = _dry_run_argv("devmain", self.env, f"DEVMAIN_REPO={OVERRIDE_REPO}")
        self.assertEqual(_flag(argv, "--repo"), OVERRIDE_REPO)

    def test_stops_without_a_target_repository(self) -> None:
        """An unset gh default stops make before gh pr create can run."""
        for env, overrides in (
            (_fake_gh_env(self.bin_dir.name, ""), ()),
            (self.env, ("DEVMAIN_REPO=",)),
        ):
            with self.subTest(overrides=overrides):
                result = _run_make("--dry-run", "devmain", *overrides, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("gh repo set-default <owner>/<repo>", result.stderr)
                self.assertNotIn("gh pr create", result.stdout)

    def test_promotes_dev_into_main(self) -> None:
        self.assertEqual(_flag(self.argv, "--base"), "main")
        self.assertEqual(_flag(self.argv, "--head"), "dev")

    def test_title_is_conventional_and_distinct_from_the_release_pr(self) -> None:
        title = _flag(self.argv, "--title")
        self.assertEqual(title, PROMOTION_TITLE)
        self.assertNotIn("release", title)

    def test_body_states_the_merge_strategy(self) -> None:
        """Squashing the promotion loses the subjects release-please reads.

        The directives are asserted with their polarity. A body that merely
        mentions both "merge commit" and "squash" could be telling the
        maintainer the opposite of what it should.
        """
        body = (_flag(self.argv, "--body") or "").lower()
        self.assertRegex(body, r"merge this pr with a merge commit")
        self.assertRegex(body, r"do not squash")
        self.assertNotRegex(body, r"squash this pr|do not (use a )?merge commit")
        # The reason, so an edit cannot reduce the body to a bare directive.
        self.assertIn("release-please", body)

    def test_does_not_infer_branches_or_text_from_the_checkout(self) -> None:
        """--fill or a bare create would derive content from local state."""
        self.assertNotIn("--fill", self.argv)
        self.assertNotIn("--fill-verbose", self.argv)

    def test_failure_is_not_swallowed(self) -> None:
        """gh's nonzero exit and its stderr must reach the operator."""
        self.assertNotIn("|| true", self.recipe)
        self.assertNotIn("2>/dev/null", self.recipe)

    def test_is_phony(self) -> None:
        self.assertIn("devmain", _phony_targets())

    def test_is_listed_by_make_help(self) -> None:
        self.assertIn("devmain", _make("help"))

    def test_verification_targets_never_open_a_pull_request(self) -> None:
        for target in VERIFICATION_TARGETS:
            with self.subTest(target=target):
                self.assertNotIn("gh pr create", _make("--dry-run", target))


class GuardSuiteReachabilityTests(unittest.TestCase):
    """`make test` reaches this suite.

    CI runs these tests as the `adr-guard-tests` job, but that job is selected
    by the `adr_guard` quality unit, whose paths do not include the Makefile
    (the Makefile is a typed `config` exclusion, and the contract rejects a
    path matched by both a unit and an exclusion). A Makefile-only change
    therefore does not select the job in CI, so the clean-checkout entrypoint
    has to run the suite for the promotion invariants above to be reachable at
    all from a Makefile edit.
    """

    def test_make_test_runs_the_repository_guard_suite(self) -> None:
        """The lane must actually run the suite, not merely name the path.

        Asserting the runner invocation rather than the bare path is what
        distinguishes a real lane from a recipe that echoes the directory. The
        suite runs under `coverage run` so SonarCloud gets an adr_guard coverage
        report (#998); the ``(?s)`` flag lets the match span the recipe's
        line-continuations between ``coverage run`` and the discover invocation.
        """
        recipe = _make("--dry-run", "test")
        self.assertRegex(
            recipe,
            r"(?s)coverage run\b.*-m unittest discover -s scripts/adr_guard/tests -p 'test_\*\.py'",
        )


class PromotionTitleLintTests(unittest.TestCase):
    """`Lint PR title` reports on the PR `devmain` opens.

    The context is a required status check on `main` as well as `dev`. A
    workflow that cannot trigger for a base branch never reports, and a
    required context that never reports blocks the merge indefinitely.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = ADR_GUARD._dw_load_workflow(REPO_ROOT, ".github/workflows/pr-title-lint.yml")
        cls.lint_step = next(
            step
            for step in cls.workflow["jobs"]["lint-title"]["steps"]
            if "semantic-pull-request" in str(step.get("uses", ""))
        )

    def test_triggers_on_every_branch_that_requires_the_context(self) -> None:
        branches = self.workflow["on"]["pull_request"]["branches"]
        for branch in TITLE_LINT_BRANCHES:
            with self.subTest(branch=branch):
                self.assertIn(branch, branches)

    def test_main_targeting_titles_satisfy_the_configured_grammar(self) -> None:
        """Every title that now reaches the lint on `main` must pass it.

        The promotion title comes from `devmain`; the release title is
        release-please's, and linting `main` newly puts it in scope.
        """
        with_config = self.lint_step["with"]
        for title in (PROMOTION_TITLE, RELEASE_PR_TITLE):
            with self.subTest(title=title):
                prefix, separator, subject = title.partition(": ")
                self.assertTrue(separator, "title must be `<type>(<scope>): <subject>`")
                commit_type = prefix.split("(", 1)[0]

                self.assertIn(commit_type, with_config["types"].split())
                self.assertRegex(subject, with_config["subjectPattern"])


if __name__ == "__main__":
    unittest.main()
