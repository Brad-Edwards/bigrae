---
id: PLAT-241
title: "Admin layer stays cloud-agnostic and uses proven components"
status: ACTIVE
type: CONSTRAINT
priority: MUST
created_at: 2026-08-01T17:37:46.207763Z
updated_at: 2026-08-01T17:41:19.898058Z
---

# PLAT-241: Admin layer stays cloud-agnostic and uses proven components

## Statement

The organization/workspace administration layer shall function identically on AWS and GCP deployments, with no cloud-specific assumptions in the SPA or the shared platform/API layer; cloud-specific behavior shall live behind the existing provider seams. The layer shall rely on proven, existing components - Django authentication/session, DRF, the existing workspace role seam and shared.audit, signed-token email for invitations, and the generated OpenAPI types and TanStack Query data layer in the SPA - rather than hand-rolling authentication, authorization, audit, or egress enforcement. Deep or rarely used administration shall remain reachable through the Django admin escape hatch rather than duplicated in the SPA.


ADR-066 S4 clarification: principal-aware account administration, membership/ownership
facts, scoped profile/lifecycle commands and canonical DRF/browser adapters are
prepared without replacing production routes or legacy authority. The coordinated
S8 activation in #2321 binds these paths and maps historical facts. Prepared
Django-admin writes are disabled in favor of the canonical validated commands.

## Rationale

The user requires a cloud-agnostic admin layer (AWS and GCP) that leans on proven components and cloud-native patterns instead of hand-rolled mechanisms. Capturing this as a cross-cutting constraint linked to every admin-layer issue makes it a durable acceptance criterion rather than per-issue prose.

## Traceability

- CONSTRAINS → GITHUB_ISSUE `1938`
- CONSTRAINS → GITHUB_ISSUE `1939`
- CONSTRAINS → GITHUB_ISSUE `1940`
- CONSTRAINS → GITHUB_ISSUE `1941`
- CONSTRAINS → GITHUB_ISSUE `1942`
- CONSTRAINS → GITHUB_ISSUE `1943`
- CONSTRAINS → GITHUB_ISSUE `1944`
- CONSTRAINS → GITHUB_ISSUE `1945`
- CONSTRAINS → GITHUB_ISSUE `1946`
- CONSTRAINS → GITHUB_ISSUE `1947`
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/api/policy_profile_views.py` (Reuses DRF, principal credentials, and the policy service seam without a cloud branch)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_policy_context.py` (Read-only projection through the workspaces facade and shared authorization port)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/app/nav.ts` (Reuses the central nav registry + Django-admin escape hatch; no cloud-specific assumptions)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/api/account-administration.ts` (Shared fetch client, generated types, and TanStack Query without cloud assumptions)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_policy_admin.py` (Human/service principal DRF boundary with no cloud-specific branch)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/api/account_admin_views.py` (Principal policy lifecycle adapter through workspaces services)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/administer/accounts/AccountAdministrationPage.tsx` (Cloud-neutral account and subdivision administration)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_policy_invitations.py` (S4 prepared administration; activation remains coordinated)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/admin_policy.py` (S4 prepared administration; activation remains coordinated)

- TESTS → TEST `shifter/shifter_platform/tests/management/test_policy_admin_preparation.py` (S4 prepared administration; activation remains coordinated)
