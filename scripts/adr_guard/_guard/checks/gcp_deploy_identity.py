"""GCP deploy-identity hygiene in the committed Helm chart values (ADR-004-R14, ADR-011)."""

from __future__ import annotations

import re
from pathlib import Path

from .._common import Violation, _read_text_safe
from .cloud_identifiers import _IDENTIFIER_RULE_ID, _identifier_skip, _iter_identifier_candidates

# ---------------------------------------------------------------------------
# ADR-004-R14 / ADR-011: GCP deploy-identity hygiene in the chart values.
#
# The Helm chart is the only GCP Kubernetes renderer (ADR-007, #2571). Its
# committed values files must never carry real deploy identity - the GCP
# project, Artifact Registry roots, or workload service-account emails. Real
# identity is reconnaissance-sensitive and deployment-specific. The deploy
# command renders it from the Terraform outputs and the release manifest into
# private generated values at deploy time, so the committed values stay on the
# shared `placeholder-project` placeholder. This is the GCP complement to the
# AWS-shaped `no-live-cloud-identifiers` scan in cloud_identifiers.py.
#
# Detection is GCP-context anchored: a project id is only inspected when it sits
# in a container-registry root (`...docker.pkg.dev/<project>/`, `gcr.io/<project>/`)
# or a service-account email (`<sa>@<project>.iam.gserviceaccount.com`). Anchoring
# on GCP syntax - not a bare `prod-*` word pattern - avoids colliding with the
# many legitimate `prod-<word>` strings elsewhere in the repo.
_GCP_DEPLOY_IDENTITY_CHECK = "no-live-gcp-deploy-identity"
_GCP_CHART_VALUES_RE = re.compile(r"^platform/charts/shifter/values[^/]*\.ya?ml$")
# The chart's universal placeholder project; it is always safe.
_GCP_SHARED_PLACEHOLDER_PROJECT = "placeholder-project"
_ALLOWED_PROJECTS = frozenset({_GCP_SHARED_PLACEHOLDER_PROJECT})
# A GCP project id (6-30 chars: lowercase letter, then letters/digits/hyphens, no
# trailing hyphen) embedded in a container-registry root or a service-account
# email. `docker.pkg.dev` matches as a substring of `<region>-docker.pkg.dev`.
_GCP_PROJECT = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"
_GCP_REGISTRY_PROJECT_RE = re.compile(r"(?:docker\.pkg\.dev|gcr\.io)/(?P<project>" + _GCP_PROJECT + r")/")
_GCP_SA_PROJECT_RE = re.compile(r"@(?P<project>" + _GCP_PROJECT + r")\.iam\.gserviceaccount\.com")


def _scan_gcp_identity_line(line: str, allowed: frozenset[str]) -> list[str]:
    """Return the GCP deploy-identity KINDs on a line whose project is not allowed."""
    kinds: list[str] = []
    for match in _GCP_REGISTRY_PROJECT_RE.finditer(line):
        if match.group("project") not in allowed:
            kinds.append("GCP Artifact Registry project")
    for match in _GCP_SA_PROJECT_RE.finditer(line):
        if match.group("project") not in allowed:
            kinds.append("GCP service-account project")
    return kinds


def check_no_live_gcp_deploy_identity(repo_root: Path, files: list[str] | None) -> list[Violation]:
    """Forbid real GCP deploy identity in the committed chart values (ADR-004-R14).

    Scans tracked ``platform/charts/shifter/values*.yaml`` files for a GCP project
    id embedded in a container-registry root or a service-account email whose
    project is not the shared ``placeholder-project``. Real identity is rendered
    at deploy time into private generated values, never committed. Detection is
    by GCP-context pattern; messages redact the value.
    """
    violations: list[Violation] = []
    for rel in _iter_identifier_candidates(repo_root, files):
        if not _GCP_CHART_VALUES_RE.match(rel) or _identifier_skip(rel):
            continue
        path = repo_root / rel
        if not path.is_file():
            continue
        text = _read_text_safe(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for kind in _scan_gcp_identity_line(line, _ALLOWED_PROJECTS):
                violations.append(
                    Violation(
                        check=_GCP_DEPLOY_IDENTITY_CHECK,
                        rule_id=_IDENTIFIER_RULE_ID,
                        path=rel,
                        message=(
                            f"line {lineno}: live {kind} in committed GCP chart values "
                            "(value redacted); keep the `placeholder-project` placeholder - "
                            "the deploy command renders real deploy identity into private "
                            "generated values at deploy time"
                        ),
                    )
                )
    return violations
