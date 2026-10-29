---
id: PLAT-231
title: "Organization/workspace administration console shell"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
created_at: 2026-08-01T17:36:46.302686Z
updated_at: 2026-08-01T17:41:19.898009Z
---

# PLAT-231: Organization/workspace administration console shell

## Statement

The SPA shall provide a staff-gated organization/workspace administration console as a first-class navigation area, with client-side routing, a workspace context/switcher backed by a current-principal context endpoint that returns the caller's organization, workspaces, and workspace roles, and a persistent full-page escape hatch to Django admin. The console shell shall be feature-flag gated and shall host the organization, workspace, membership, invitation, user-lifecycle, range-scoping, policy, quota, and audit surfaces as child routes.

ADR-066 S8 clarification: the production console is policy-gated rather than
staff-gated and starts from the current principal's account context. Account,
organization and workspace selection now stays inside the account administration
surface; the retired workspace-context switcher and rollout flag are not parallel
authority paths. The full-page Django administration escape hatch remains separate.

## Rationale

The workspace tenancy layer (PLAT-2011, program #1321) is API-only; the SPA exposes none of it. A shared shell provides the routing, principal context, and navigation the per-capability admin slices hang off, and preserves the existing Django-admin escape hatch already present in the SPA nav.

## Traceability

- IMPLEMENTS → GITHUB_ISSUE `1938`
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/api/policy_profile_views.py` (Current-principal account context through the policy-native API)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_policy_context.py` (Policy-filtered principal account projection)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/administer/accounts/AccountAdministrationPage.tsx` (Account, organization, workspace, membership, and policy console)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/api/account-administration.ts` (Generated-contract TanStack Query administration client)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/router.tsx` (Policy-native account administration route and legacy-route redirect)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/app/nav.ts` (Policy-derived account administration navigation)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_policy_admin.py` (Account-context and policy-native administration API tests)
- TESTS → TEST `shifter/shifter_platform/frontend/src/features/administer/accounts/AccountAdministrationPage.test.tsx` (Account console context, selection, and mutation tests)
- TESTS → TEST `shifter/shifter_platform/frontend/src/app/nav.test.ts` (Policy-derived navigation visibility tests)
