"""Run the existing changed-line quality gate without recursive dependency walks."""

from __future__ import annotations

import fnmatch
import glob
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_INCLUDES = (
    "shifter/shifter_platform/**",
    "shifter/engine/provisioner/**",
    "shifter/engine/openvpn/**",
    "shifter/packer/**",
    "shifter/installation/**",
    "scripts/bootstrap/**",
    "scripts/check_layer_imports/**",
    "scripts/adr_guard/**",
)
SOURCE_EXCLUDES = ("**/tests/**", "**/test_*.py", "**/conftest.py", "**/migrations/**", "**/documentation/**")
RUFF_OPTIONS = (
    "--isolated --line-length 120 "
    "--select C901,PLR0911,PLR0913,D101,D102,D103,ANN001,ANN002,ANN003,ANN201,ANN202,ANN204,ANN401,E501 "
    "--config lint.mccabe.max-complexity=10 "
    "--config lint.pylint.max-returns=3 "
    "--config lint.pylint.max-args=7"
)
QUALITY_COMMAND = (
    "diff-quality",
    "--violations=ruff.check",
    "--compare-branch=origin/dev",
    "--fail-under=100",
    f"--options={RUFF_OPTIONS}",
)
GIT_DIFF_COMMANDS = (
    ("git", "diff", "--name-only", "-z", "--end-of-options", "origin/dev...HEAD", "--"),
    ("git", "diff", "--name-only", "-z", "--cached", "--"),
    ("git", "diff", "--name-only", "-z", "--"),
)


def changed_paths(root: Path) -> set[str]:
    """Read committed, staged and unstaged Git paths using the gate's diff semantics."""
    paths: set[str] = set()
    for command in GIT_DIFF_COMMANDS:
        result = subprocess.run(
            command,
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        )
        paths.update(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)
    return paths


def _source_roots(patterns: Sequence[str]) -> tuple[str, ...]:
    """Accept the hook's literal source-root/** patterns, refusing unsupported scope changes."""
    roots = []
    for pattern in patterns:
        root = pattern.removesuffix("/**")
        if (
            not pattern.endswith("/**")
            or not Path(root).parts
            or glob.has_magic(root)
            or Path(root).is_absolute()
            or ".." in Path(root).parts
        ):
            raise ValueError(f"Expected a relative literal source-root/** pattern: {pattern}")
        roots.append(Path(root).as_posix() + "/")
    return tuple(roots)


def _inside_source(path: str, roots: Sequence[str]) -> bool:
    """Match recursive root scope while preserving glob's omission of hidden paths."""
    return any(
        path.startswith(root) and all(not part.startswith(".") for part in path[len(root) :].split("/"))
        for root in roots
    )


def _excluded(root: Path, path: str, patterns: Sequence[str]) -> bool:
    """Preserve diff-quality's basename, repository-relative and absolute fnmatch exclusions."""
    return any(
        fnmatch.fnmatch(candidate, pattern)
        for pattern in patterns
        for candidate in (os.path.basename(path), path, str(root / path))
    )


def selected_paths(root: Path, paths: set[str], includes: Sequence[str], excludes: Sequence[str]) -> list[str]:
    """Select existing changed Python files without walking unrelated filesystem trees."""
    roots = _source_roots(includes)
    return sorted(
        path
        for path in paths
        if path.lower().endswith(".py")
        and _inside_source(path, roots)
        and not _excluded(root, path, excludes)
        and (root / path).is_file()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixed hook policy; reject external arguments before any subprocess."""
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("This fixed-purpose quality hook accepts no arguments.", file=sys.stderr)
        return 2
    paths = selected_paths(REPO_ROOT, changed_paths(REPO_ROOT), SOURCE_INCLUDES, SOURCE_EXCLUDES)
    if not paths:
        print("No eligible changed Python files in the configured quality scope.")
        return 0
    command = [
        *QUALITY_COMMAND,
        "--include",
        *(glob.escape(path) for path in paths),
        "--exclude",
        *SOURCE_EXCLUDES,
    ]
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
