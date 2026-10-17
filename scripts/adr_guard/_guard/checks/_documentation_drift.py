"""Documentation manifest validation and source-to-handbook pairing."""

from __future__ import annotations

import json
import os
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .._common import Violation, _boundary_mock_base_reference_candidates, _git_text

DOCS_COVERAGE_MANIFEST = "docs/adr/documentation-coverage.yaml"
DOCS_COVERAGE_RULE_ID = "ADR-022-R1"
_DOCS_EXCLUDED_PART = "_deprecated"


def _doc_path_is_excluded(slug: str) -> bool:
    """A doc under a deprecated or hidden path part is not serveable."""
    return any(
        part == _DOCS_EXCLUDED_PART or part.startswith(".")
        for part in slug.split("/") if part
    )


def _drift_violation(message: str) -> Violation:
    """Report pairing and manifest errors through the existing ADR rule."""
    return Violation("documentation-coverage", "ADR-022-R2", DOCS_COVERAGE_MANIFEST, message)


def _association(feature: dict[str, object]) -> dict[str, object]:
    """Project the stable fields shared by current and base mappings."""
    return {key: feature.get(key) for key in ("id", "drift_sources", "technical_docs")}


def _drift_associations(
    repo_root: Path, manifest: dict[str, object]
) -> tuple[list[dict[str, object]], list[Violation]]:
    """Retain base mappings so a policy edit cannot erase its own obligation."""
    associations: list[dict[str, object]] = []
    errors: list[Violation] = []
    seen: set[str] = set()
    for feature in manifest["features"]:
        if not isinstance(feature, dict) or "drift_sources" not in feature:
            continue
        identifier = feature.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            errors.append(_drift_violation("Missing or duplicate drift feature id"))
        else:
            seen.add(identifier)
        associations.append(_association(feature))
    base, base_errors = _base_drift_associations(repo_root)
    errors.extend(base_errors)
    for association in base:
        if association in associations:
            continue
        if isinstance(association["drift_sources"], list):
            association["drift_sources"] = [*association["drift_sources"], DOCS_COVERAGE_MANIFEST]
        associations.append(association)
    return associations, errors


def _read_base_drift_manifest(repo_root: Path) -> tuple[str | None, list[Violation]]:
    """Distinguish first enrollment from an unreadable base artifact."""
    refs = _boundary_mock_base_reference_candidates(repo_root)
    raw = _git_text(repo_root, ["show", f"{refs[0]}:{DOCS_COVERAGE_MANIFEST}"]) if refs else None
    errors: list[Violation] = []
    if refs and raw is None:
        listing = _git_text(repo_root, ["ls-tree", "--name-only", refs[0], "--", DOCS_COVERAGE_MANIFEST])
        if listing is None or listing.strip():
            errors.append(_drift_violation("Cannot read base documentation drift associations"))
    return raw, errors


def _base_drift_associations(repo_root: Path) -> tuple[list[dict[str, object]], list[Violation]]:
    """Read immutable base associations using the incumbent Git helpers."""
    raw, errors = _read_base_drift_manifest(repo_root)
    associations: list[dict[str, object]] = []
    if raw is not None:
        try:
            base = json.loads(raw)
            associations = [
                _association(feature) for feature in base["features"]
                if isinstance(feature, dict) and "drift_sources" in feature
            ]
        except (ValueError, KeyError, TypeError):
            errors.append(_drift_violation("Cannot parse base documentation drift associations"))
    return associations, errors


def _relative_drift_path(value: object) -> bool:
    """Accept repository-relative paths/globs without traversal or controls."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    return not (
        value.startswith("/") or ".." in PurePosixPath(value).parts or "\\" in value
        or any(ord(char) < 32 for char in value)
    )


def _drift_site_exclusions(repo_root: Path) -> tuple[list[str], list[Violation]]:
    """BaseLoader handles MkDocs custom tags without running constructors."""
    import yaml

    path = repo_root / "mkdocs.yml"
    try:
        config = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        return [], [_drift_violation("Cannot read MkDocs configuration for drift targets")]
    if not isinstance(config, dict) or not isinstance(config.get("exclude_docs", ""), str):
        return [], [_drift_violation("Cannot validate MkDocs exclusions for drift targets")]
    return (config.get("exclude_docs") or "").splitlines(), []


def _target_is_excluded(target: str, excludes: list[str]) -> bool:
    """Match hidden paths and the current MkDocs exclusion patterns."""
    return _doc_path_is_excluded(target) or any(
        fnmatchcase(target, pattern.strip()) or target.startswith(pattern.strip().rstrip("/") + "/")
        for pattern in excludes if pattern.strip()
    )


def _check_drift_targets(
    repo_root: Path, docs_root: str, feature: dict[str, object]
) -> list[Violation]:
    """Require contained public pages, using this site's exclusion patterns."""
    targets = feature.get("technical_docs")
    if not isinstance(targets, list) or not targets or not all(isinstance(target, str) for target in targets):
        return [_drift_violation("Drift associations require technical_docs")]
    excludes, errors = _drift_site_exclusions(repo_root)
    if errors:
        return errors
    root = (repo_root / docs_root).resolve()
    for target in targets:
        if not _relative_drift_path(target) or not (root / target).resolve().is_relative_to(root):
            errors.append(_drift_violation("Drift technical_docs must be repository-contained paths"))
        elif _target_is_excluded(target, excludes):
            errors.append(_drift_violation(f"Drift technical_docs target is excluded from MkDocs: {target}"))
    return errors


def _read_documentation_diff(
    repo_root: Path, reference: str, enforce: bool
) -> tuple[set[str], list[Violation]]:
    """Read NUL-delimited changed and untracked paths without a shell."""
    diff = _git_text(repo_root, ["diff", "--name-only", "-z", "--no-renames", reference, "--"])
    untracked = _git_text(repo_root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if diff is None or untracked is None:
        errors = [_drift_violation("Cannot read the base diff for handbook drift")] if enforce else []
        return set(), errors
    return set(filter(None, diff.split("\0"))) | set(filter(None, untracked.split("\0"))), []


def _documentation_changed_paths(
    repo_root: Path, files: list[str] | None, manifest: dict[str, object]
) -> tuple[set[str], list[Violation]]:
    """Use supplied local paths or the complete PR diff, including deletions."""
    if files is not None:
        return set(files), []
    refs = _boundary_mock_base_reference_candidates(repo_root)
    enforce = os.environ.get("ADR_GUARD_SNAPSHOT_ENFORCE", "").lower() in {"1", "true", "yes"}
    if not refs and enforce:
        return set(), [_drift_violation("Cannot resolve the base for handbook drift; fetch base history")]
    result: tuple[set[str], list[Violation]] = (set(), [])
    if any(isinstance(f, dict) and "drift_sources" in f for f in manifest["features"]):
        result = _read_documentation_diff(repo_root, refs[0] if refs else "HEAD", enforce)
    return result


def _check_feature_drift(
    feature: dict[str, object], docs_root: str, changed: set[str]
) -> list[Violation]:
    """Require review of the mapped technical page when its boundary changes."""
    if "drift_sources" not in feature:
        return []
    sources = feature["drift_sources"]
    errors: list[Violation] = []
    if not isinstance(sources, list) or not sources or not all(_relative_drift_path(source) for source in sources):
        errors.append(_drift_violation(
            f"feature {feature.get('id')} drift_sources must be nonempty repo-relative globs"
        ))
    else:
        affected = any(fnmatchcase(path, source) for path in changed for source in sources)
        targets = {f"{docs_root}/{rel}" for rel in (feature.get("technical_docs") or [])}
        if affected and not targets.intersection(changed):
            errors.append(_drift_violation(
                f"feature {feature.get('id')} boundary drift requires review/update of: {', '.join(sorted(targets))}"
            ))
    return errors


def _coverage_manifest_shape_error(manifest: object) -> str | None:
    """Return the shape error for a loaded coverage manifest, or None when valid."""
    if not isinstance(manifest, dict):
        return "documentation coverage manifest must be a JSON object"
    if not isinstance(manifest.get("docs_root"), str) or not isinstance(
        manifest.get("features"), list
    ):
        return "manifest must define a string 'docs_root' and a list of 'features'"
    return None


def _feature_doc_paths_error(feature: dict[str, object]) -> str | None:
    """Validate path-list shape separately from coverage and pairing."""
    error = None
    for field in ("user_docs", "technical_docs"):
        paths = feature.get(field) or []
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            error = f"feature {feature.get('id')} {field} must be a list of paths"
            break
    return error


def _coverage_violation(path: str, message: str) -> Violation:
    """Build a documentation-coverage violation for ``path``."""
    return Violation("documentation-coverage", DOCS_COVERAGE_RULE_ID, path, message)
