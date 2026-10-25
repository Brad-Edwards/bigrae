"""Extend CKV_GCP_125's repository grammar without changing its decisions."""

from __future__ import annotations

import importlib
import re
import sys
from importlib.metadata import version

# The pinned action uses image tag 3.2.495 with package version 3.2.494.
CHECKOV_VERSION = "3.2.494"
LEGACY_PATTERN = r"(\$\{)?[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*(\})?/[^/]+"
_NAME = r"(?:[a-zA-Z0-9_-]+|\$\{[a-zA-Z_][a-zA-Z0-9_.]*\})"
_ID = r"(?:[1-9][0-9]*|\$\{[a-zA-Z_][a-zA-Z0-9_.]*\})"
IMMUTABLE_PATTERN = rf"{_NAME}@{_ID}/{_NAME}@{_ID}$"
EXTENDED_PATTERN = rf"(?:{IMMUTABLE_PATTERN}|{LEGACY_PATTERN})"


def repository_pattern(installed_version: str, current_pattern: str) -> re.Pattern[str]:
    """Extend only the pinned, known parser and allow repeat loader imports."""
    if installed_version != CHECKOV_VERSION:
        raise RuntimeError(
            "unsupported Checkov version for immutable subject compatibility"
        )
    if current_pattern not in {LEGACY_PATTERN, EXTENDED_PATTERN}:
        raise RuntimeError("unexpected Checkov repository parser")
    return re.compile(EXTENDED_PATTERN)


def activate() -> None:
    """Patch only the vendor GCP check's imported repository pattern."""
    policy = importlib.import_module(
        "checkov.terraform.checks.resource.gcp.GithubActionsOIDCTrustPolicy"
    )
    policy.gh_repo_regex = repository_pattern(
        version("checkov"), policy.gh_repo_regex.pattern
    )


# Checkov imports external modules after loading its built-in checks. Normal
# unit-test imports remain independent of the optional scanner dependency.
if "checkov.main" in sys.modules:
    try:
        activate()
    except Exception as error:
        # Checkov logs and suppresses normal external-loader exceptions.
        # SystemExit preserves a failing process result on compatibility drift.
        raise SystemExit(
            "Checkov immutable subject compatibility initialization failed"
        ) from error
