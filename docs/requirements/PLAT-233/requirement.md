---
id: PLAT-233
title: "Workspace lifecycle management"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
created_at: 2026-08-01T17:36:56.293306Z
updated_at: 2026-08-01T17:41:19.898022Z
---

# PLAT-233: Workspace lifecycle management

## Statement

During S1, the platform shall retain the existing workspace lifecycle APIs (create, list/search, rename, archive/restore, and owner transfer) and SPA surface for legacy organization workspaces under the current workspace service authority. In the new account hierarchy, each team or enterprise organization shall have exactly one default workspace that cannot be archived while designated as default, and the domain service shall support additional workspaces without implicit membership grants. Individual accounts shall have no workspace. Archival shall not delete ranges bound to a workspace. Personal compatibility workspaces remain legacy storage until the coordinated S8 cutover and are not defaults of individual accounts.


ADR-066 S4 clarification: principal-aware account administration, membership/ownership
facts, scoped profile/lifecycle commands and canonical DRF/browser adapters are
prepared without replacing production routes or legacy authority. The coordinated
S8 activation in #2321 binds these paths and maps historical facts. Prepared
Django-admin writes are disabled in favor of the canonical validated commands.

ADR-066 S8 supersession: Production workspace creation, discovery, rename,
archive, restore and ownership commands use live principal policy and explicit
account ancestry. Retained IDs and range bindings remain stable. Creation
records exact parent publication intent and supports receipt-bound recovery.
Individual accounts have no workspaces. This supersedes the S1 and S4
coexistence boundary above.

## Rationale

Workspace lifecycle is the core of a usable shared-infrastructure admin layer. ADR-066 replaces the per-user personal-workspace default with typed account hierarchy while preserving existing workspace IDs and archival behavior. The S1 account creation service establishes dormant structure; S8 must converge it with the lifecycle API and principal membership in one authority cutover.

## Traceability

- IMPLEMENTS → ADR `docs/adr/066-account-principal-resource-scope.md` (Account hierarchy and S8 supersession)
- DOCUMENTS → DOCUMENTATION `docs/architecture/workspace-lifecycle-preflight-1940.md` (Workspace lifecycle activation boundary)
- IMPLEMENTS → GITHUB_ISSUE `2314` (Typed defaults and account ancestry)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_account.py` (Default workspace creation without implicit grants)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_hierarchy.py` (Idempotent defaults, additional workspaces, and no implicit membership)
- IMPLEMENTS → GITHUB_ISSUE `1940`
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_lifecycle.py` (Transactional workspace lifecycle; last-owner, personal-workspace, and default archive invariants; reversible archived_at marker)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/api/lifecycle_views.py` (Session-authorized DRF workspace lifecycle API under /api/v1/workspaces/)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_workspace.py` (archived_at reversible archive marker; archival never cascades to bound ranges)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/roles.py` (Workspace lifecycle operations and role matrix (owner/admin read/rename/archive/restore; owner-only transfer))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/administer/accounts/AccountAdministrationPage.tsx` (Policy-filtered organization and workspace collection surface)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/administer/accounts/WorkspaceAdministrationPanel.tsx` (Workspace rename, archive/restore, owner transfer, and membership surface)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_lifecycle.py` (Lifecycle service behavior, authorization, and invariant tests (last-owner, no range cascade, personal protection))
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_policy_admin.py` (Policy-native lifecycle HTTP boundary and opaque denials)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_policy_workspace_lifecycle.py` (Principal policy lifecycle, invariant, and audit tests)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_policy_memberships.py` (S4 prepared administration; activation remains coordinated)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/administer/accounts/WorkspaceAdministrationPanel.tsx` (S4 prepared administration; activation remains coordinated)

- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_policy_memberships.py` (S4 prepared administration; activation remains coordinated)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_account_policy_lifecycle.py` (Prepared lifecycle and fact-only integer ownership compatibility)
- IMPLEMENTS → GITHUB_ISSUE `2321` (S8 activation of the prepared policy-native workspace lifecycle)
- TESTS → TEST `shifter/shifter_platform/frontend/src/features/administer/accounts/WorkspaceAdministrationPanel.test.tsx` (Activated workspace lifecycle UI contract)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_hierarchy_publication.py` (Resumable publication of created workspace ancestry)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_prepared_admin_routes.py` (Recovery of workspace creation after policy publication failure)
- TESTS → TEST `shifter/shifter_platform/frontend/e2e/journeys/workspace-lifecycle.spec.ts` (Create, rename, archive and restore through native account administration and released policy service)
