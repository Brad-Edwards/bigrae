"""Preserve changed-line gate scope without recursively walking dependencies."""

from __future__ import annotations

import glob
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "changed_python_quality", ROOT / "scripts/adr_guard/changed_python_quality.py"
)
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)

EXCLUDES = ["**/tests/**", "**/test_*.py", "**/conftest.py", "**/migrations/**", "**/documentation/**"]


@pytest.fixture
def repository_root(tmp_path):
    # Absolute fnmatch exclusions also see parent directories. Keep pytest's
    # test_* fixture name outside this synthetic source root's ancestor path.
    root = tmp_path.parent / ("quality-" + tmp_path.name.removeprefix("test_"))
    root.mkdir()
    return root


def test_nested_scope_and_exclusions_match_existing_glob_behavior(repository_root):
    tmp_path = repository_root
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
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic fixture\n", encoding="utf-8")
    paths.add("owned/deep/deleted.py")
    expected = ["owned/deep/nested/module.py", "owned/root.py"]
    assert QUALITY.selected_paths(tmp_path, paths, ["owned/**"], EXCLUDES) == expected
    original = {str(Path(path).relative_to(tmp_path)) for path in glob.glob(str(tmp_path / "owned/**"), recursive=True)}
    assert set(expected) <= original
    assert "owned/.hidden/module.py" not in original


@pytest.mark.parametrize("pattern", ["owned/*", "../owned/**", "/owned/**", "owned/[ab]/**", "/**", "./**"])
def test_unsupported_source_pattern_fails_closed(tmp_path, pattern):
    with pytest.raises(ValueError, match="literal source-root"):
        QUALITY.selected_paths(tmp_path, set(), [pattern], [])


def test_all_three_git_diffs_preserve_nul_delimited_names(tmp_path, monkeypatch):
    calls = []
    responses = iter([b"owned/committed.py\0", b"owned/staged name.py\0", b"owned/unstaged\nname.py\0"])

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(QUALITY.subprocess, "run", run)
    assert QUALITY.changed_paths(tmp_path, "origin/dev") == {
        "owned/committed.py",
        "owned/staged name.py",
        "owned/unstaged\nname.py",
    }
    assert [command for command, _ in calls] == [
        ["git", "diff", "--name-only", "-z", "origin/dev...HEAD"],
        ["git", "diff", "--name-only", "-z", "--cached"],
        ["git", "diff", "--name-only", "-z"],
    ]
    assert all(options["check"] and options["cwd"] == tmp_path for _, options in calls)


def test_literal_names_rules_and_failure_status_reach_real_cli_boundary(repository_root, monkeypatch):
    tmp_path = repository_root
    name = "owned/nested/name [x]*?\nfile.py"
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_text("# synthetic fixture\n", encoding="utf-8")
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(stdout=name.encode() + b"\0", returncode=7)

    monkeypatch.setattr(QUALITY, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(QUALITY.subprocess, "run", run)
    rules = ["--violations=ruff.check", "--fail-under=100", "--options=--isolated --select ANN201"]
    assert QUALITY.main([*rules, "--include", "owned/**", "--exclude", *EXCLUDES]) == 7
    command, options = calls[-1]
    assert command[:4] == ["diff-quality", *rules]
    literal = command[command.index("--include") + 1]
    assert literal == glob.escape(name)
    assert glob.glob(str(tmp_path / literal)) == [str(path)]
    assert command[command.index("--exclude") + 1 :] == EXCLUDES
    assert options == {"cwd": tmp_path, "check": False}


def test_empty_scope_does_not_lint_unrelated_files(tmp_path, monkeypatch):
    calls = []

    def run(command, **options):
        calls.append(command)
        return SimpleNamespace(stdout=b"other/module.py\0")

    monkeypatch.setattr(QUALITY, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(QUALITY.subprocess, "run", run)
    assert QUALITY.main(["--violations=ruff.check", "--fail-under=100", "--include", "owned/**"]) == 0
    assert len(calls) == 3 and all(command[0] == "git" for command in calls)


def test_git_failure_cannot_be_treated_as_empty_scope(tmp_path, monkeypatch):
    def fail(command, **options):
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(QUALITY, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(QUALITY.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        QUALITY.main(["--violations=ruff.check", "--fail-under=100", "--include", "owned/**"])


def test_hook_keeps_pinned_tools_and_zero_tolerance():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hook = next(hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "sonar-rules-changed-lines")
    assert hook["entry"] == "python scripts/adr_guard/changed_python_quality.py"
    assert hook["additional_dependencies"] == ["diff-cover==11.0.0", "ruff==0.15.18"]
    assert "--fail-under=100" in hook["args"]
    assert hook["pass_filenames"] is False
