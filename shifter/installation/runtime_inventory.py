"""Inventory and validate repo runtime configuration surfaces.

The root installation config (``shifter.yaml``) remains the public
operator-authored contract.  This module records the checked-in runtime
configuration surfaces and validates their key-level shape without ever
printing values. On GCP the shared Helm chart is the only renderer (#2571): the
deploy command renders the runtime env into private generated values at deploy
time, so the committed chart values must not carry renderer-owned env keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# The AWS sets moved to runtime_inventory_aws (backend symmetry with the GCP
# split). They are re-exported here so the established public import surface
# stays stable — scripts/bootstrap/aws_eks.py, engine.ecs, and the AWS runtime
# role parity test import them from installation.runtime_inventory. The names in
# __all__ mark them as intentional re-exports.
from .runtime_inventory_aws import (
    AWS_EKS_REQUIRED_RUNTIME_ENV_KEYS,
    AWS_PROVISIONER_FORWARDED_RUNTIME_ENV_KEYS,
    AWS_RENDERER_OWNED_RUNTIME_ENV_KEYS,
)

# The per-backend key inventories were split into sibling modules for file size
# (S104) and backend symmetry. This module imports only the GCP sets it uses
# internally; other consumers (registry, the GCP parity test) import the full GCP
# set directly from runtime_inventory_gcp.
from .runtime_inventory_gcp import (
    GCP_CAPACITY_RUNTIME_ENV_KEYS,
    GCP_GENERATED_RUNTIME_ENV_KEYS,
    GCP_HELM_RENDERED_RUNTIME_ENV_KEYS,
    GCP_OPTIONAL_GENERATED_RUNTIME_ENV_KEYS,
    GCP_SECRET_RUNTIME_ENV_KEYS,
)

__all__ = [
    "AWS_EKS_REQUIRED_RUNTIME_ENV_KEYS",
    "AWS_PROVISIONER_FORWARDED_RUNTIME_ENV_KEYS",
    "AWS_RENDERER_OWNED_RUNTIME_ENV_KEYS",
    "GCP_BACKEND_OWNER",
    "GCP_CAPACITY_RUNTIME_ENV_KEYS",
    "GCP_CHART_VALUES_PATHS",
    "GCP_GENERATED_RUNTIME_ENV_KEYS",
    "GCP_HELM_RENDERED_RUNTIME_ENV_KEYS",
    "GCP_SECRET_RUNTIME_ENV_KEYS",
    "RUNTIME_SURFACES",
    "RuntimeInventoryIssue",
    "RuntimeSurface",
    "env_keys_from_file",
    "validate_runtime_inventory",
]

_ENV_KEY_RE = re.compile(r"^([A-Za-z_]\w*)=", re.ASCII)

# The committed chart values that a GCP deployment renders with: the shared defaults
# and one file for each supported GCP profile.
GCP_CHART_VALUES_PATHS: tuple[Path, ...] = (
    Path("platform/charts/shifter/values.yaml"),
    Path("platform/charts/shifter/values-gcp-dev.yaml"),
    Path("platform/charts/shifter/values-gcp-prod.yaml"),
)
GCP_BACKEND_OWNER = "gcp backend"
# Keys that a deploy-time renderer owns. A committed chart value for one of them
# would survive a renderer change as a stale value.
_GCP_RENDERER_OWNED_RUNTIME_ENV_KEYS: frozenset[str] = (
    GCP_GENERATED_RUNTIME_ENV_KEYS
    | GCP_OPTIONAL_GENERATED_RUNTIME_ENV_KEYS
    | GCP_CAPACITY_RUNTIME_ENV_KEYS
    | GCP_HELM_RENDERED_RUNTIME_ENV_KEYS
)


@dataclass(frozen=True)
class RuntimeSurface:
    """A checked-in or operator-local runtime configuration surface."""

    path: str
    owner: str
    authority: str
    notes: str


@dataclass(frozen=True)
class RuntimeInventoryIssue:
    """A sanitized inventory validation problem.

    ``message`` must mention only file paths and env-key names, never values.
    """

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


RUNTIME_SURFACES: tuple[RuntimeSurface, ...] = (
    RuntimeSurface(
        path="shifter.yaml",
        owner="installation",
        authority="operator-authored root installation config",
        notes="Public backend/deployment/settings contract; validated by shifter-config validate.",
    ),
    RuntimeSurface(
        path=".shifter.yaml",
        owner="mcp/ops",
        authority="checked-in MCP ops policy namespace",
        notes="Per-tool policy for the shifter-ops MCP server, not deployment secret storage.",
    ),
    *(
        RuntimeSurface(
            path=str(path),
            owner=GCP_BACKEND_OWNER,
            authority="checked-in chart values for GCP renders",
            notes=(
                "Non-secret chart values; the deploy command renders the runtime env into private "
                "generated values at deploy time, so no renderer-owned env key is committed here."
            ),
        )
        for path in GCP_CHART_VALUES_PATHS
    ),
    RuntimeSurface(
        path=".env",
        owner="local operator",
        authority="gitignored local environment",
        notes="Local-only workstation values and secret references; never part of the checked-in runtime contract.",
    ),
    RuntimeSurface(
        path="shifter/shifter_platform/.env",
        owner="local developer",
        authority="gitignored local platform environment",
        notes="Local-only Django/compose values and secrets; use .env.example for checked-in shape.",
    ),
)


def _repo_path(repo_root: Path, relative_path: Path) -> Path:
    """Resolve a repo-relative runtime inventory path."""

    return repo_root / relative_path


def env_keys_from_file(path: Path) -> tuple[str, ...]:
    """Return assignment keys from an env file without exposing assignment values."""

    keys: list[str] = []
    # Normalize the path (collapsing any `..` traversal) before reading so the
    # bytes scanned come from the resolved target rather than the raw input.
    resolved = path.resolve()
    for line in resolved.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_KEY_RE.match(stripped)
        if match is not None:
            keys.append(match.group(1))
    return tuple(keys)


def _missing_file_issue(path: Path) -> RuntimeInventoryIssue:
    """Return a sanitized issue for a missing runtime inventory file."""

    return RuntimeInventoryIssue(str(path), "required runtime inventory file is missing")


def _chart_runtime_env_issues(path: Path, relative_path: Path) -> list[RuntimeInventoryIssue]:
    """Return sanitized issues for renderer-owned env keys committed in chart values."""

    try:
        values = yaml.safe_load(path.resolve().read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return [RuntimeInventoryIssue(str(relative_path), "chart values file is not valid YAML")]
    if not isinstance(values, dict):
        return [RuntimeInventoryIssue(str(relative_path), "chart values file must be a mapping")]
    runtime_env = values.get("runtimeEnv") or {}
    if not isinstance(runtime_env, dict):
        return [RuntimeInventoryIssue(str(relative_path), "runtimeEnv must be a mapping")]
    committed = sorted(str(key) for key in runtime_env if key in _GCP_RENDERER_OWNED_RUNTIME_ENV_KEYS)
    if not committed:
        return []
    return [
        RuntimeInventoryIssue(
            str(relative_path),
            "renderer-owned env keys must not be committed in chart values: " + ", ".join(committed),
        )
    ]


def validate_runtime_inventory(repo_root: str | Path) -> list[RuntimeInventoryIssue]:
    """Validate checked-in runtime configuration surfaces by path and key name only."""

    root = Path(repo_root)
    issues: list[RuntimeInventoryIssue] = []
    for relative_path in GCP_CHART_VALUES_PATHS:
        path = _repo_path(root, relative_path)
        if not path.is_file():
            issues.append(_missing_file_issue(relative_path))
            continue
        issues.extend(_chart_runtime_env_issues(path, relative_path))
    return issues
