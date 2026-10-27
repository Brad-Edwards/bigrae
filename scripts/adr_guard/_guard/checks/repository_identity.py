"""ADR-069: no repository identity literals in executable or configuration files.

The upstream repository name, its immutable IDs, and its registry owner
namespace live in one declaration, docs/adr/repository-identity.json. Code that
needs its own identity reads the runtime context; trust anchors read deployment
configuration; tooling reads the clone's gh default repository. This check
fails when a tracked file repeats the declared identity, or a former name,
outside the places that ADR-069-R5 allows.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

from .._common import Violation, _git_tracked_all, _read_text_safe, _walk_all_files
from .._repository_identity import (
    REPOSITORY_IDENTITY_REL,
    RepositoryIdentity,
    RepositoryIdentityError,
    load_repository_identity,
)

_RULE_ID = "ADR-069-R6"
_CHECK = "repository-identity-literals"

# ADR-069-R5: documentation, the documentation site, and historical links may
# name the upstream project.
_REFERENCE_PREFIXES = ("docs/",)
_REFERENCE_SUFFIXES = (".md",)
_REFERENCE_FILES = frozenset({"mkdocs.yml"})
# ADR-069-R5: provenance records keep the name the content came from.
_PROVENANCE_GLOBS = (
    "shifter/shifter_platform/cms/scenarios/inbox_packs/manifest.yaml",
    "shifter/environment-packs/*/associated-artifacts.json",
)
# ADR-069-R3: per-environment deployment configuration states the trusted repository.
_DEPLOYMENT_CONFIG_SUFFIXES = (".tfvars",)
# Checked separately: it must equal the declaration.
_GROUND_CONTROL_REL = ".ground-control.yaml"
_SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".woff", ".woff2")
_SKIP_BASENAMES = frozenset({"package-lock.json", "uv.lock"})
# ADR-069-R2: an upstream-only workflow guard compares the immutable IDs.
_WORKFLOW_GLOB = ".github/workflows/*.yml"


def _is_allowed_path(rel: str) -> bool:
    """True for paths where ADR-069-R5 or R3 allows the identity."""
    return (
        rel in (REPOSITORY_IDENTITY_REL, _GROUND_CONTROL_REL)
        or rel.startswith(_REFERENCE_PREFIXES)
        or rel.endswith(_REFERENCE_SUFFIXES)
        or rel in _REFERENCE_FILES
        or rel.endswith(_DEPLOYMENT_CONFIG_SUFFIXES)
        or any(fnmatch.fnmatch(rel, pattern) for pattern in _PROVENANCE_GLOBS)
    )


def _skip(rel: str) -> bool:
    """True for binary or lock files the check must not scan."""
    return rel.rsplit("/", 1)[-1] in _SKIP_BASENAMES or rel.endswith(_SKIP_SUFFIXES)


def _patterns(identity: RepositoryIdentity) -> list[tuple[re.Pattern[str], str]]:
    """Identity forms to find, each with a kind for the message."""
    boundary_before, boundary_after = r"(?<![A-Za-z0-9_.-])", r"(?![A-Za-z0-9_.-])"
    patterns = [
        (re.compile(boundary_before + re.escape(name) + boundary_after, re.IGNORECASE), kind)
        for name, kind in [(identity.repository, "repository name")]
        + [(former, "former repository name") for former in identity.former_repositories]
    ]
    patterns.extend(
        [
            (re.compile(rf"(?<!\d){identity.repository_id}(?!\d)"), "repository ID"),
            (re.compile(rf"(?<!\d){identity.owner_id}(?!\d)"), "owner ID"),
            (
                re.compile(r"ghcr\.io/" + re.escape(identity.owner.lower()) + r"/", re.IGNORECASE),
                "registry owner namespace",
            ),
        ]
    )
    return patterns


def _workflow_guard_mask(identity: RepositoryIdentity) -> re.Pattern[str]:
    """The ADR-069-R2 upstream-only guard comparisons that a workflow may hold."""
    return re.compile(
        rf"github\.repository_id\s*==\s*'{identity.repository_id}'"
        rf"|github\.repository_owner_id\s*==\s*'{identity.owner_id}'"
    )


def _violation(rel: str, message: str) -> Violation:
    """Build an ADR-069-R6 violation for a path with a plain message."""
    return Violation(check=_CHECK, rule_id=_RULE_ID, path=rel, message=message)


def _scan_file(path: Path, rel: str, identity: RepositoryIdentity) -> list[Violation]:
    """Return every identity-literal violation in one tracked file."""
    text = _read_text_safe(path)
    if text is None:
        return []
    mask = _workflow_guard_mask(identity) if fnmatch.fnmatch(rel, _WORKFLOW_GLOB) else None
    violations = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        scanned = mask.sub("", line) if mask else line
        for pattern, kind in _patterns(identity):
            if pattern.search(scanned):
                violations.append(
                    _violation(
                        rel,
                        f"line {lineno}: {kind} literal. Use the runtime context, deployment "
                        "configuration, or the gh default repository (ADR-069)",
                    )
                )
    return violations


def _ground_control_violations(repo_root: Path, identity: RepositoryIdentity) -> list[Violation]:
    """Fail when .ground-control.yaml github_repo differs from the declaration."""
    path = repo_root / _GROUND_CONTROL_REL
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return [_violation(_GROUND_CONTROL_REL, "cannot read the file to compare github_repo")]
    configured = data.get("github_repo") if isinstance(data, dict) else None
    mismatch = configured is not None and configured != identity.repository
    message = f"github_repo must equal 'repository' in {REPOSITORY_IDENTITY_REL}"
    return [_violation(_GROUND_CONTROL_REL, message)] if mismatch else []


def _candidates(repo_root: Path, files: list[str] | None) -> list[str]:
    """The paths to scan: the given files, or every tracked file."""
    if files is not None:
        return list(files)
    tracked = _git_tracked_all(repo_root)
    return tracked if tracked is not None else _walk_all_files(repo_root)


def check_repository_identity_literals(repo_root: Path, files: list[str] | None) -> list[Violation]:
    """Fail on repository identity literals outside the ADR-069 allowed places."""
    try:
        identity = load_repository_identity(repo_root)
    except RepositoryIdentityError as exc:
        return [_violation(REPOSITORY_IDENTITY_REL, str(exc))]
    violations = _ground_control_violations(repo_root, identity)
    for rel in _candidates(repo_root, files):
        if _skip(rel) or _is_allowed_path(rel):
            continue
        path = repo_root / rel
        if path.is_file():
            violations.extend(_scan_file(path, rel, identity))
    return violations
