"""Preserve changed-line gate scope without recursively walking dependencies."""

from __future__ import annotations

import glob
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "changed_python_quality", ROOT / "scripts/adr_guard/changed_python_quality.py"
)
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)

EXCLUDES = ["**/tests/**", "**/test_*.py", "**/conftest.py", "**/migrations/**", "**/documentation/**"]


class ChangedPythonQualityTests(unittest.TestCase):
    """The native ADR unittest lane exercises every wrapper boundary case."""

    def setUp(self) -> None:
        # Absolute fnmatch exclusions see parent names, so avoid a test_* directory.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="changed-quality-")))

    def test_nested_scope_and_exclusions_match_existing_glob_behavior(self) -> None:
        paths = {
            "owned/root.py",
            "owned/deep/nested/module.py",
            "owned/tests/test_module.py",
            "owned/deep/migrations/0001.py",
            "owned/documentation/example.py",
            "owned/conftest.py",
            "owned/.hidden/module.py",
            "owned/deep/data.json",
            "owned_extra/module.py",
            "other/module.py",
        }
        for name in paths | {"owned/node_modules/untracked.py"}:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic fixture\n", encoding="utf-8")
        paths.add("owned/deep/deleted.py")
        expected = ["owned/deep/nested/module.py", "owned/root.py"]
        self.assertEqual(QUALITY.selected_paths(self.root, paths, ["owned/**"], EXCLUDES), expected)
        original = {
            str(Path(path).relative_to(self.root)) for path in glob.glob(str(self.root / "owned/**"), recursive=True)
        }
        self.assertLessEqual(set(expected), original)
        self.assertNotIn("owned/.hidden/module.py", original)

    def _assert_unsupported_pattern(self, pattern: str) -> None:
        with self.assertRaisesRegex(ValueError, "literal source-root"):
            QUALITY.selected_paths(self.root, set(), [pattern], [])

    def test_nonrecursive_source_pattern_fails_closed(self) -> None:
        self._assert_unsupported_pattern("owned/*")

    def test_parent_source_pattern_fails_closed(self) -> None:
        self._assert_unsupported_pattern("../owned/**")

    def test_absolute_source_pattern_fails_closed(self) -> None:
        self._assert_unsupported_pattern("/owned/**")

    def test_wildcard_source_root_fails_closed(self) -> None:
        self._assert_unsupported_pattern("owned/[ab]/**")

    def test_empty_source_root_fails_closed(self) -> None:
        self._assert_unsupported_pattern("/**")

    def test_current_directory_source_root_fails_closed(self) -> None:
        self._assert_unsupported_pattern("./**")

    def test_all_three_git_diffs_preserve_nul_delimited_names(self) -> None:
        calls = []
        responses = iter([b"owned/committed.py\0", b"owned/staged name.py\0", b"owned/unstaged\nname.py\0"])

        def run(command, **options):
            calls.append((command, options))
            return SimpleNamespace(stdout=next(responses))

        with patch.object(QUALITY.subprocess, "run", run):
            self.assertEqual(
                QUALITY.changed_paths(self.root, "origin/dev"),
                {"owned/committed.py", "owned/staged name.py", "owned/unstaged\nname.py"},
            )
        self.assertEqual(
            [command for command, _ in calls],
            [
                ["git", "diff", "--name-only", "-z", "origin/dev...HEAD"],
                ["git", "diff", "--name-only", "-z", "--cached"],
                ["git", "diff", "--name-only", "-z"],
            ],
        )
        self.assertTrue(all(options["check"] and options["cwd"] == self.root for _, options in calls))

    def test_literal_names_rules_and_failure_status_reach_real_cli_boundary(self) -> None:
        name = "owned/nested/name [x]*?\nfile.py"
        path = self.root / name
        path.parent.mkdir(parents=True)
        path.write_text("# synthetic fixture\n", encoding="utf-8")
        calls = []

        def run(command, **options):
            calls.append((command, options))
            return SimpleNamespace(stdout=name.encode() + b"\0", returncode=7)

        rules = ["--violations=ruff.check", "--fail-under=100", "--options=--isolated --select ANN201"]
        with patch.object(QUALITY, "REPO_ROOT", self.root), patch.object(QUALITY.subprocess, "run", run):
            self.assertEqual(QUALITY.main([*rules, "--include", "owned/**", "--exclude", *EXCLUDES]), 7)
        command, options = calls[-1]
        self.assertEqual(command[:4], ["diff-quality", *rules])
        literal = command[command.index("--include") + 1]
        self.assertEqual(literal, glob.escape(name))
        self.assertEqual(glob.glob(str(self.root / literal)), [str(path)])
        self.assertEqual(command[command.index("--exclude") + 1 :], EXCLUDES)
        self.assertEqual(options, {"cwd": self.root, "check": False})

    def test_empty_scope_does_not_lint_unrelated_files(self) -> None:
        calls = []

        def run(command, **options):
            calls.append(command)
            return SimpleNamespace(stdout=b"other/module.py\0")

        with patch.object(QUALITY, "REPO_ROOT", self.root), patch.object(QUALITY.subprocess, "run", run):
            self.assertEqual(QUALITY.main(["--violations=ruff.check", "--fail-under=100", "--include", "owned/**"]), 0)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(command[0] == "git" for command in calls))

    def test_git_failure_cannot_be_treated_as_empty_scope(self) -> None:
        def fail(command, **options):
            raise subprocess.CalledProcessError(128, command)

        with (
            patch.object(QUALITY, "REPO_ROOT", self.root),
            patch.object(QUALITY.subprocess, "run", fail),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            QUALITY.main(["--violations=ruff.check", "--fail-under=100", "--include", "owned/**"])

    def test_hook_keeps_pinned_tools_and_zero_tolerance(self) -> None:
        config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
        hook = next(
            hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "sonar-rules-changed-lines"
        )
        self.assertEqual(hook["entry"], "python scripts/adr_guard/changed_python_quality.py")
        self.assertEqual(hook["additional_dependencies"], ["diff-cover==11.0.0", "ruff==0.15.18"])
        self.assertIn("--fail-under=100", hook["args"])
        self.assertIs(hook["pass_filenames"], False)


if __name__ == "__main__":
    unittest.main()
