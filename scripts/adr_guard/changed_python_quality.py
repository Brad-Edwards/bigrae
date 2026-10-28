"""Run the existing changed-line quality gate without recursive dependency walks."""

from __future__ import annotations

import argparse
import fnmatch
import glob
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def changed_paths(root: Path, compare_branch: str) -> set[str]:
    """Read committed, staged and unstaged Git paths using the gate's diff semantics."""
    paths: set[str] = set()
    for revision in ((f"{compare_branch}...HEAD",), ("--cached",), ()):
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", *revision],
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
    """Pass literal eligible paths and unchanged rules to the pinned diff-quality CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare-branch", default="origin/dev")
    parser.add_argument("--include", nargs="+", required=True)
    parser.add_argument("--exclude", nargs="+", default=[])
    options, quality_args = parser.parse_known_args(argv)
    paths = selected_paths(
        REPO_ROOT, changed_paths(REPO_ROOT, options.compare_branch), options.include, options.exclude
    )
    if not paths:
        print("No eligible changed Python files in the configured quality scope.")
        return 0
    command = [
        "diff-quality",
        *quality_args,
        "--compare-branch",
        options.compare_branch,
        "--include",
        *(glob.escape(path) for path in paths),
    ]
    if options.exclude:
        command.extend(["--exclude", *options.exclude])
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
