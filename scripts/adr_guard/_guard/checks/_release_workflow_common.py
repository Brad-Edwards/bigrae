"""Shared names and helpers of the release-workflow-provenance check (#2571, #2660)."""

from __future__ import annotations

from .._common import Violation
from ._deploy_workflow_action_pin import _dw_step_uses

RELEASE_WORKFLOW_PATH = ".github/workflows/release-gcp.yml"
RELEASE_PLEASE_WORKFLOW_PATH = ".github/workflows/release-please.yml"
CHECK = "release-workflow-provenance"


def _violation(rule_id: str, message: str, path: str = RELEASE_WORKFLOW_PATH) -> Violation:
    """Build one violation of this check, against the release workflow by default."""
    return Violation(CHECK, rule_id, path, message)


def _is_true(value: object) -> bool:
    """True for a YAML boolean true or the string ``"true"``."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _step_with(step: object) -> dict[str, object]:
    """Return a step's ``with:`` mapping, or an empty mapping."""
    if isinstance(step, dict) and isinstance(step.get("with"), dict):
        return step["with"]
    return {}


def _uses_action(step: object, action: str) -> bool:
    """True when the step uses ``action`` at any ref."""
    return (_dw_step_uses(step) or "").startswith(action)
