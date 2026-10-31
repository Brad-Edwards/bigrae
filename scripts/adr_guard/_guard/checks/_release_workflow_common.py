"""Shared names and helpers of the release-workflow-provenance check (#2571, #2660)."""

from __future__ import annotations

import re

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


# A ref guard is a shell `case` on GITHUB_REF whose only admitted branch lists
# the allowed refs and whose fallback branch exits non-zero.
_REF_GUARD_CASE = re.compile(
    r'case\s+"\$\{GITHUB_REF\}"\s+in\s+(?P<refs>[^)\s]+)\)\s*;;\s*\*\)(?P<fallback>.*?);;\s*esac',
    re.DOTALL,
)


def _rejects_refs_outside(step: object, allowed: frozenset[str]) -> bool:
    """True when the step always runs and fails on every ref outside ``allowed``."""
    if not isinstance(step, dict) or "if" in step or _is_true(step.get("continue-on-error")):
        return False
    script = step.get("run")
    if not isinstance(script, str):
        return False
    match = _REF_GUARD_CASE.search(script)
    return bool(
        match and set(match.group("refs").split("|")) <= allowed and re.search(r"\bexit\s+1\b", match.group("fallback"))
    )


def _environment_name(job: dict[str, object]) -> str:
    """Return the job's GitHub Environment name, or ``""`` when it binds none."""
    environment = job.get("environment")
    if isinstance(environment, dict):
        environment = environment.get("name")
    return environment.strip() if isinstance(environment, str) else ""


def _trigger_events(on: object) -> set[str]:
    """Return the event names of a workflow ``on:`` value."""
    if isinstance(on, str):
        return {on}
    if isinstance(on, (list, dict)):
        return {str(event) for event in on}
    return set()
