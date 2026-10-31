#!/usr/bin/env python3
"""Enforce generic GCP purpose trust and resolved-plan verification (ADR-004-R23, ADR-070).

GitHub federation is optional. Operators reach the lifecycle identities through
impersonation by named principals; this guard keeps that grant closed too.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from ._resolved_plan import check_resolved_plan as check_resolved_plan
else:
    from _resolved_plan import check_resolved_plan as check_resolved_plan

# The promote purpose is retired with packer-gcp-promote.yml (#2668).
PURPOSES = ("build", "validate", "release_scan", "deploy", "destroy")
# Purposes that operators may impersonate without GitHub (ADR-070).
IMPERSONATION_PURPOSES = ("release_scan", "deploy", "destroy")
# Purpose identities that must hold no project-wide IAM role.
NO_PROJECT_ROLE_PURPOSES = ("release_scan",)
PROJECT_IAM_MEMBER_RE = re.compile(r'^\s*resource\s+"google_project_iam_member"\s+"([^\"]+)"\s*\{')
VARIABLE_HEADER_RE = re.compile(r'^\s*variable\s+"([^\"]+)"\s*\{')
DOUBLE_QUOTED_RE = re.compile(r'"([^\"]+)"')
OUTPUT_RE = re.compile(r'^\s*output\s+"([^\"]+)"\s*\{', re.MULTILINE)
REQUIRED_OUTPUTS = frozenset({
    "workload_identity_provider", "packer_build_service_account_email",
    "packer_validate_service_account_email",
    "release_scan_service_account_email", "deploy_service_account_email", "destroy_service_account_email",
})
# The variables that keep the static CKV_GCP_125 grammar check meaningful. A
# default would let the check evaluate a concrete value instead of the immutable
# placeholder, and would let a fork federate by omission (ADR-069).
NO_DEFAULT_TRUST_VARIABLES = ("github_repository_id", "github_owner_id", "purpose_contexts")
SA_IAM_MEMBER_RE = re.compile(r'resource "google_service_account_iam_member" "([^"]+)" \{(.*?)\n\}', re.S)
# Service-account roles that let a principal act as, or mint tokens for, an identity.
IMPERSONATION_ROLES = ("roles/iam.serviceAccountTokenCreator", "roles/iam.serviceAccountUser")
IMPERSONATION_BINDING = (
    "for_each = local.impersonation_grants",
    "service_account_id = local.service_account_names[each.value.purpose]",
    'role = "roles/iam.serviceAccountTokenCreator"',
    "member = each.value.member",
)
IMPERSONATION_PURPOSE_RULE = 'contains(["release_scan", "deploy", "destroy"], purpose) && length(members) > 0'
IMPERSONATION_MEMBER_RULE = 'can(regex("^(user|group|serviceAccount):'
# Build may act as itself for the image export; no other self or cross grant exists.
BUILD_SELF_GRANTS = frozenset({"packer_build_act_as_self", "packer_build_token_creator_self"})

# One generic template for arbitrary validated contexts. The resolved-plan
# verifier checks the emitted policy and full binding set independently before
# apply. Keeping this source shape closed also rejects local bypasses before
# an operator ever handles an inventory or cloud credential.
PROVIDER_EXPRESSION = '''"assertion.repository == '${var.github_org}/${var.github_repo}' && assertion.repository_id == '${var.github_repository_id}' && assertion.repository_owner_id == '${var.github_owner_id}' && assertion.event_name == 'workflow_dispatch' && (${join(" || ", flatten([
    for purpose, contexts in var.purpose_contexts : [
      for context in contexts : "(assertion.sub == '${local.subject_prefix}:environment:${context.environment}' && assertion.ref == '${context.ref}' && assertion.workflow_ref == '${context.workflow_ref}'${context.reusable_workflow_ref == "" ? "" : " && assertion.job_workflow_ref == '${context.reusable_workflow_ref}'"})"
    ]
  ]))})"'''


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def check_generic_source(path: Path, text: str) -> list[Violation]:
    clean = _strip_hcl_comments(text)
    errors = []
    if "checkov:skip=CKV_GCP_125" in text.replace(" ", ""):
        errors.append("CKV_GCP_125 must remain blocking")
    if 'resource "google_iam_workload_identity_pool_provider"' not in clean:
        return [Violation(path, 1, reason) for reason in errors]
    match = re.search(r"attribute_condition\s*=\s*(.*?)\n\s*oidc\s*\{", clean, re.S)
    if not match or _compact(match[1]) != _compact(PROVIDER_EXPRESSION):
        errors.append("Provider must render the exact generic repository/owner/subject/ref/workflow tuple contract")
    if 'issuer_uri = "https://token.actions.githubusercontent.com"' not in _compact(clean):
        errors.append("Provider must use the GitHub OIDC issuer")
    if "principalSet://" in clean or "allowed_audiences" in clean:
        errors.append("Repository-wide principals and alternate audiences are forbidden")
    subject_prefix = 'subject_prefix = var.github_subject_format == "immutable" ? "repo:${var.github_org}@${var.github_owner_id}/${var.github_repo}@${var.github_repository_id}" : "repo:${var.github_org}/${var.github_repo}"'
    if _compact(subject_prefix) not in _compact(clean):
        errors.append("Subject prefix must derive from the reviewed format and immutable repository IDs")
    subject_map = '''for purpose in ["build", "validate", "release_scan", "deploy", "destroy"] :
    purpose => distinct([for context in lookup(var.purpose_contexts, purpose, []) : "${local.subject_prefix}:environment:${context.environment}"])'''
    if _compact(subject_map) not in _compact(clean):
        errors.append("All purpose subjects must derive from the validated context mapping")
    principal = 'sub => "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${var.name_prefix}-github/subject/${sub}"'
    if _compact(principal) not in _compact(clean):
        errors.append("Bindings must derive exact subject principals from the verified foundation project")
    for purpose in PURPOSES:
        name = "packer_build_wif" if purpose == "build" else purpose + "_wif"
        match = re.search(rf'resource "google_service_account_iam_member" "{name}" \{{(.*?)\n\}}', clean, re.S)
        if not match or not all(token in _compact(match[1]) for token in [
            f"for_each = local.purpose_subject_principals.{purpose}",
            f"service_account_id = local.service_account_names.{purpose}",
            'role = "roles/iam.workloadIdentityUser"', "member = each.value",
        ]):
            errors.append(f"{purpose} must bind only its own subjects and service account")
    for match in re.finditer(r'resource "google_project_iam_(?:member|custom_role)" "[^\"]+" \{(.*?)\n\}', clean, re.S):
        if "project = var.project_id" not in _compact(match[1]):
            errors.append("CI capabilities must target the configured deployment project")
    errors.extend(_impersonation_errors(clean))
    return [Violation(path, 1, reason) for reason in errors]


def _impersonation_errors(clean: str) -> list[str]:
    """Keep every act-as and token grant closed: build self grants and named operators only."""
    errors = []
    for name, body in SA_IAM_MEMBER_RE.findall(clean):
        compact = _compact(body)
        if not any(f'role = "{role}"' in compact for role in IMPERSONATION_ROLES) or name in BUILD_SELF_GRANTS:
            continue
        if name != "impersonation" or not all(token in compact for token in IMPERSONATION_BINDING):
            errors.append(
                f"{name}: impersonation grants must use the impersonation resource with named principals for "
                "their own purpose identity only (ADR-070)"
            )
    return errors


def check_trust_variables(path: Path, text: str) -> list[Violation]:
    """Keep the federation anchors default-free and the impersonation principals closed."""
    if path.parent.name not in {"cicd-oidc-identity", "cicd-oidc"}:
        return []
    clean = _strip_hcl_comments(text)
    errors = []
    for _, block in _iter_resource_blocks(clean.splitlines(), VARIABLE_HEADER_RE):
        name = VARIABLE_HEADER_RE.match(block[0]).group(1)  # type: ignore[union-attr]
        body = _compact("\n".join(block))
        if name in NO_DEFAULT_TRUST_VARIABLES and re.search(r"\bdefault\s*=", body):
            errors.append(f"{name} must have no default; a deployment without GitHub sets it explicitly (ADR-069)")
        if name == "impersonation_principals" and path.parent.name == "cicd-oidc-identity" and not (
            IMPERSONATION_PURPOSE_RULE in body and IMPERSONATION_MEMBER_RULE in body
        ):
            errors.append(
                "impersonation_principals must admit only release_scan, deploy, and destroy, each with "
                "user:, group:, or serviceAccount: members (ADR-070)"
            )
    return [Violation(path, 1, reason) for reason in errors]


def check_file(path: Path) -> list[Violation]:
    if path.suffix != ".tf":
        return []
    text = path.read_text(encoding="utf-8")
    module_text = text
    if path.parent.name == "cicd-oidc-identity":
        module_text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(path.parent.glob("*.tf")))
    errors = check_generic_source(path, text)
    errors.extend(check_role_boundaries(path, module_text.splitlines()))
    errors.extend(check_output_contract(path, text))
    errors.extend(check_trust_variables(path, text))
    return errors


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(__file__).resolve().parents[2]
    paths = (
        [Path(p) for p in args]
        if args
        else sorted(
            [
                *root.glob("platform/terraform/gcp/modules/cicd-oidc-identity/*.tf"),
                *root.glob("platform/terraform/gcp/global/cicd-oidc/*.tf"),
            ]
        )
    )
    errors = [error for path in paths for error in check_file(path)]
    for error in errors:
        print(error)
    return int(bool(errors))


@dataclass
class Violation:
    file: Path
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.reason}"


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _extract_resource_block(lines: list[str], start_idx: int) -> list[str]:
    depth = _brace_delta(lines[start_idx])
    idx = start_idx + 1
    while idx < len(lines) and depth > 0:
        depth += _brace_delta(lines[idx])
        idx += 1
    return lines[start_idx:idx]


def _strip_hcl_comments(text: str) -> str:
    """Drop `#` line comments so trust matching never keys on prose.

    `#` never appears inside the provider condition, principal members, or
    subjects the checks inspect, so this is a safe, precise strip. `//` / `/* */`
    stripping is intentionally avoided (a `principalSet://` member contains a
    literal `//`).
    """
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def _iter_resource_blocks(lines: list[str], header_re: re.Pattern[str]) -> list[tuple[int, list[str]]]:
    blocks: list[tuple[int, list[str]]] = []
    idx = 0
    while idx < len(lines):
        if header_re.match(lines[idx]):
            block = _extract_resource_block(lines, idx)
            blocks.append((idx + 1, block))
            idx += len(block)
            continue
        idx += 1
    return blocks


def _variable_values(lines: list[str], variable_name: str) -> set[str] | None:
    for _, block in _iter_resource_blocks(lines, VARIABLE_HEADER_RE):
        header = block[0]
        match = VARIABLE_HEADER_RE.match(header)
        if match and match.group(1) == variable_name:
            # Compare the configured default only. Descriptions and validation
            # messages are prose, not role assignments; including every quoted
            # string in the variable block lets differing descriptions make two
            # identical privilege sets appear independently derived (#2084).
            body = "\n".join(block)
            default = re.search(r"\bdefault\s*=\s*\[(?P<values>.*?)]", body, re.DOTALL)
            if default is None:
                return set()
            return set(DOUBLE_QUOTED_RE.findall(default.group("values")))
    return None


def check_role_boundaries(path: Path, lines: list[str]) -> list[Violation]:
    """Reject role/permission classes forbidden to narrow CI identities."""
    violations: list[Violation] = []
    stripped_lines = _strip_hcl_comments("\n".join(lines)).splitlines()
    for line_no, block in _iter_resource_blocks(stripped_lines, PROJECT_IAM_MEMBER_RE):
        for purpose in NO_PROJECT_ROLE_PURPOSES:
            if re.search(rf"member\s*=.*local\.service_account_emails\.{purpose}\b", "\n".join(block)):
                violations.append(
                    Violation(
                        path,
                        line_no,
                        "release-scan identity must have no project-wide IAM role; use repository-scoped grants (#2084, ADR-070)",
                    )
                )
    platform_roles = _variable_values(stripped_lines, "platform_roles")
    deploy_roles = _variable_values(stripped_lines, "deploy_roles")
    destroy_roles = _variable_values(stripped_lines, "destroy_roles")
    module_text = "\n".join(stripped_lines)
    has_lifecycle_identities = (
        'resource "google_service_account" "deploy"' in module_text
        or 'resource "google_service_account" "destroy"' in module_text
    )
    if has_lifecycle_identities and (platform_roles is not None or deploy_roles is None or destroy_roles is None):
        violations.append(
            Violation(
                path,
                1,
                "platform lifecycle identities must use separate deploy_roles and destroy_roles variables (#2084)",
            )
        )
    if deploy_roles is not None and destroy_roles is not None:
        forbidden_lifecycle_roles = {
            "roles/compute.admin",
            "roles/compute.imageAdmin",
            "roles/compute.instanceAdmin.v1",
            "roles/compute.storageAdmin",
            "roles/editor",
            "roles/owner",
            "roles/storage.admin",
        }
        for purpose, roles in (
            ("deploy", deploy_roles),
            ("destroy", destroy_roles),
        ):
            if overlap := roles & forbidden_lifecycle_roles:
                violations.append(
                    Violation(
                        path,
                        1,
                        f"{purpose} role set contains release-evidence-bypassing broad roles {sorted(overlap)} (#2084)",
                    )
                )
        if deploy_roles == destroy_roles:
            violations.append(
                Violation(
                    path,
                    1,
                    "deploy and destroy role sets must be independently derived (#2084)",
                )
            )
        if "roles/serviceusage.serviceUsageAdmin" in destroy_roles:
            violations.append(
                Violation(
                    path,
                    1,
                    "destroy role set must not enable or disable project services (#2084)",
                )
            )
    validate_roles = _variable_values(stripped_lines, "validate_roles")
    if validate_roles is not None:
        forbidden = {
            "roles/compute.admin",
            "roles/storage.admin",
            "roles/cloudbuild.builds.editor",
            "roles/iam.serviceAccountAdmin",
            "roles/resourcemanager.projectIamAdmin",
        }
        if overlap := validate_roles & forbidden:
            violations.append(
                Violation(
                    path,
                    1,
                    f"validate role set contains forbidden broad roles {sorted(overlap)} (#1699)",
                )
            )

    validate_permissions = _variable_values(stripped_lines, "validate_permissions")
    if validate_permissions is not None:
        forbidden = {
            "compute.images.create",
            "compute.images.delete",
            "compute.images.deprecate",
        }
        if overlap := validate_permissions & forbidden:
            violations.append(
                Violation(
                    path,
                    1,
                    f"validate permission set crosses image-build/promotion authority {sorted(overlap)} (#1699)",
                )
            )

    build_roles = _variable_values(stripped_lines, "build_roles")
    if build_roles is not None and "roles/storage.admin" in build_roles:
        violations.append(
            Violation(
                path,
                1,
                "build role set must use resource-scoped GCS grants, not roles/storage.admin (#1699)",
            )
        )
    return violations


def check_output_contract(path: Path, text: str) -> list[Violation]:
    """Purpose identity module must expose every explicit secret value."""
    if path.name != "outputs.tf" or path.parent.name != "cicd-oidc-identity":
        return []
    outputs = set(OUTPUT_RE.findall(_strip_hcl_comments(text)))
    missing = REQUIRED_OUTPUTS - outputs
    if not missing:
        return []
    return [
        Violation(
            path,
            1,
            f"GCP CI identity module must publish explicit purpose outputs; missing {sorted(missing)} (#1699)",
        )
    ]

if __name__ == "__main__":
    raise SystemExit(main())
