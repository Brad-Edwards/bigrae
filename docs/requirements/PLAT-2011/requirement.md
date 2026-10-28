---
id: PLAT-2011
title: "Organization/workspace tenancy boundary above range ownership"
status: ACTIVE
type: CONSTRAINT
priority: MUST
created_at: 2026-07-26T16:31:32.402253Z
updated_at: 2026-07-26T21:30:40.607851Z
---

# PLAT-2011: Organization/workspace tenancy boundary above range ownership

## Statement

The platform shall provide a stable typed Account root above Organization and Workspace in the bounded workspaces domain. Individual accounts shall own resources directly without organization or workspace subdivisions; team and enterprise accounts shall have a default organization, and every organization shall have a default workspace, with additional subdivisions permitted. Organization and Workspace public and internal IDs shall be preserved where ancestry is valid. New account membership shall target a stable principal and shall never arise merely from default creation; existing organization and workspace memberships retain their durable rows until the coordinated S8 cutover. The shared scope contract and owning-domain schema shall require explicit account scope with valid optional organization/workspace ancestry for new account-scoped resources; installation scope shall be explicit rather than inferred from absent customer IDs. Existing workspace-bound rows retain their scalar scope and owner identity until S8. Owning domains shall retain scalar scope references without cross-layer model imports or ForeignKeys. Ambiguous historical personal-workspace and ownership evidence shall block the coordinated S8 cutover without deletion or guessed reassignment.


ADR-066 S4 clarification: principal-aware account administration, membership/ownership
facts, scoped profile/lifecycle commands and canonical DRF/browser adapters are
prepared without replacing production routes or legacy authority. The coordinated
S8 activation in #2321 binds these paths and maps historical facts. Prepared
Django-admin writes are disabled in favor of the canonical validated commands.

ADR-066 S8 supersession: This delivery activates native principal authority and
explicit resource ancestry across production paths. Cutover converts retained
membership and resource facts through their owning domains. It verifies all
legacy memberships, including those in already-bound organizations. Ambiguous
custody or unsupported administrator roles block activation. This supersedes the
S1 and S4 coexistence boundary above.

## Rationale

The earlier per-user personal organization/workspace compatibility rule in ADR-046 cannot represent an individual without invented subdivisions. ADR-066 supersedes that rule while retaining the workspaces domain boundary, validated scalar references, and durable organization/workspace IDs. S1 adds the new schema, contracts, and mapping checks; S8 is the single production activation point for new authority and final scope enforcement.

## Traceability

- DOCUMENTS → DOCUMENTATION `docs/technical/shifter_platform/identity-lifecycle-contracts.md` (Identity and lifecycle DFDs, state ownership, recovery and threat verification)

- IMPLEMENTS → ADR `docs/adr/index.yaml` (ADR-046: organization/workspace tenancy is a domain boundary above range ownership)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_organization.py` (Organization model)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_workspace.py` (Workspace model (organization FK, unique personal_for_user))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_membership.py` (WorkspaceMembership model (unique per workspace+user, closed role))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_authorization.py` (Workspace authorization seam (immutable result, indistinguishable denials))
- IMPLEMENTS → ADR `docs/adr/066-account-principal-resource-scope.md` (Supersedes the personal-workspace default and defines account scope)
- DOCUMENTS → DOCUMENTATION `docs/architecture/account-principal-scope-preflight-2314.md` (S1 account and principal architecture boundaries)
- IMPLEMENTS → GITHUB_ISSUE `2314` (Account, hierarchy, principal, and mapping implementation slice)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_account.py` (Stable typed Account and explicit principal membership)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_account.py` (Type-correct defaults and exact ancestry resolution)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/identity_mapping.py` (Fail-closed classification of historical hierarchy and membership)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/migrations/0010_account_accountmembership_organization_is_default_and_more.py` (Forward account hierarchy schema)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/models.py` (Independent human and service principals with exact provider bindings)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/principals.py` (Principal lifecycle and bind-once provider identities)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/services.py` (Public principal service facade)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/migrations/0014_principal_providerbinding_and_more.py` (Forward principal and binding schema)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/migrations/0015_map_human_principals.py` (Durable user and complete provider-tuple mapping)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/migrations/0016_provider_binding_immutability.py` (Database-level binding immutability)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/identity_scope.py` (Explicit principal and resource scope contracts)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/migrations/0025_alter_auditlog_entity_type.py` (Audit vocabulary for account and principal mutations)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/models/provisioning.py` (Explicit account scope alongside retained workspace binding)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/models/range.py` (Explicit account scope on CMS range projection)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/migrations/0052_rangeinstance_account_id_and_more.py` (Forward CMS resource scope schema)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/models/_range.py` (Explicit account scope on Engine range)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/migrations/0087_range_account_id_range_organization_id_and_more.py` (Forward Engine resource scope schema)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/event.py` (Explicit account scope on CTF events)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/communication.py` (Explicit account scope on communication campaigns)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/migrations/0062_remove_ctfevent_ctf_event_public_registration_scoped_and_more.py` (Forward CTF resource scope schema)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_hierarchy.py` (Account hierarchy and ancestry constraints)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_legacy_account_mapping.py` (Legacy personal and shared organization mapping blockers)
- TESTS → TEST `shifter/shifter_platform/tests/management/test_principals.py` (Independent service lifecycle and provider-binding collision/immutability)
- TESTS → TEST `shifter/shifter_platform/tests/management/test_principal_mapping.py` (Exact legacy identity mapping and ambiguous-row blocker)
- TESTS → TEST `shifter/shifter_platform/tests/shared/test_identity_scope.py` (Principal reference and scope shape rejection)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_account_scope_schema.py` (Explicit scope persistence)
- IMPLEMENTS → CONFIG `scripts/check_layer_imports/layer_imports.yaml` (workspaces classified as a domain layer; facade-only access enforced)
- DOCUMENTS → DOCUMENTATION `docs/technical/shifter_platform/workspaces.md` (Workspaces domain technical documentation)
- DOCUMENTS → DOCUMENTATION `docs/technical/shifter_platform/management.md` (Principal identity technical documentation)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_models.py` (Tenancy model invariants (organization FK, unique membership, one personal workspace))
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_services.py` (Authorization seam and per-user personal workspace resolution)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_backfill_migration_schema.py` (Upgrade proven against the real historical schema (unbound rows bound, none left))
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_range_workspace_binding.py` (Range scope binding across all three ownership projections; rehome semantics)
- TESTS → TEST `shifter/shifter_platform/tests/engine/services/test_raes_range_workspace_persistence.py` (Engine RAES seam persists exact scope and refuses a missing binding)
- IMPLEMENTS → GITHUB_ISSUE `1325` (ADR + data model: organization/workspace layer above user-owned ranges)
- IMPLEMENTS → PULL_REQUEST `1863` (feat(platform): add organization/workspace tenancy above range ownership)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/roles.py` (Closed workspace role-to-operation policy)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/api/views.py` (Workspace membership lifecycle API boundary)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_memberships.py` (Transactional workspace membership lifecycle and strict audit)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/migrations/0003_alter_workspacemembership_role_and_more.py` (Closed membership role database constraint migration)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_range_access.py` (Workspace-authorized interactive range access facade)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/services/_queries.py` (Exact request-and-workspace-correlated range projection)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/api/principals.py` (Neutral active principal resolution for session and token requests)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_range_workspace.py` (CMS workspace authorization seam for range operations)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_memberships.py` (Workspace membership lifecycle, authority, invariants, and audit tests)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_account_policy_admin.py` (Policy-native account, organization, workspace, and membership HTTP boundary)
- TESTS → TEST `shifter/shifter_platform/tests/integration/engine/test_consumers_integration.py` (Range consumer and CMS correlation integration tests)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_queries.py` (CTF range correlation and membership-revocation query tests)
- TESTS → TEST `shifter/shifter_platform/tests/mission_control/test_vpn_profile_api.py` (Workspace membership revocation at VPN secret-delivery boundary)
- DOCUMENTS → DOCUMENTATION `docs/features/workspaces.md` (Workspace membership roles user documentation)
- IMPLEMENTS → GITHUB_ISSUE `1326` (Workspace membership and roles)
- DOCUMENTS → DOCUMENTATION `docs/architecture/workspace-membership-roles-preflight-1326.md` (Workspace membership roles architecture preflight)
- IMPLEMENTS → PULL_REQUEST `1916` (feat(platform): add workspace membership roles)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_raes_range_create.py` (RAES-native launch path threads the same workspace selection and admission seam)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/services/_range_backend_binding.py` (Engine idempotent-create workspace-binding replay guard (ADR-046-R9))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/mission_control/api/ranges.py` (Mission Control launch command accepts and maps the optional public workspace selection)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_raes_range_workspace_binding.py` (RAES launch retains exact workspace scope across CMS and Engine)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_range_workspace_scoping.py` (Cross-workspace denial across every interactive range lifecycle surface)
- DOCUMENTS → DOCUMENTATION `docs/architecture/range-workspace-scoping-preflight-1327.md` (Range workspace scoping architecture preflight (ADR-046-R9/R10))
- IMPLEMENTS → PULL_REQUEST `1931` (feat(platform): scope range launches to workspaces)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_ownership.py` (S4 prepared administration; activation remains coordinated)

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_policy_range_scope.py` (S4 prepared administration; activation remains coordinated)

- TESTS → TEST `shifter/shifter_platform/tests/cms/test_policy_range_scope.py` (S4 prepared administration; activation remains coordinated)
- IMPLEMENTS → GITHUB_ISSUE `2321` (Coordinated S8 identity and account-authority activation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/models/_identity_cutover.py` (Durable fail-closed S8 cutover state and policy journal)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_identity_cutover.py` (Deterministic account mapping, relationship publication, verification, and activation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/config/management/commands/identity_cutover.py` (Deployment-owned S8 cutover command)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_identity_cutover.py` (Cutover blockers, idempotency, verification, and activation tests)
- TESTS → TEST `shifter/shifter_platform/tests/config/test_identity_cutover_command.py` (Composed cutover command contract tests)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_hierarchy_publication.py` (Persisted parent-edge publication and bounded recovery)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/management/commands/reconcile_hierarchy_publications.py` (Operator recovery of interrupted hierarchy creation)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_prepared_admin_routes.py` (Mounted organization discovery and HTTP creation recovery)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_scoped_range_lifecycle.py` (Exact retained-event deletion authority and generation-bound native cleanup)
- TESTS → TEST `shifter/shifter_platform/tests/cms/services/test_principal_range_lifecycle.py` (Cleanup ownership survives removal of the retained event aggregate)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_authorization_policy.py` (Readback of exact admitted mutations cannot create or replay a provider write)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/launch_intents.py` (Durable original launch proof and cancellation shared with CMS reservation and native creation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/launch_cleanup.py` (Cleanup of interrupted launches before participant or spare association)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_content_scope_options.py` (Policy-filtered content ancestry discovery for native principals)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_range_launch_crash_recovery.py` (Process crashes, immutable intent, cancellation and PostgreSQL creator/cleanup races)
- TESTS → TEST `shifter/shifter_platform/tests/mission_control/test_principal_guacamole.py` (Current proof, source custody and PostgreSQL transfer/handoff lock order)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_content_scope_options_api.py` (Human and service content-only authority with exact ancestry and bounded paging)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/_pack_registration_custody.py` (Exact server-recorded archive custody and installation authority before source registration)
- IMPLEMENTS → CODE_FILE `scripts/gcp/enter_identity_maintenance.py` (Validated workload inventories and bounded termination before cutover)
- TESTS → TEST `scripts/gcp/tests/test_enter_identity_maintenance.py` (Inventory failures, partial maintenance failures and old-process termination)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_pack_registration_custody.py` (Human and service cross-tenant storage denial, upload lease custody and removal races)
- TESTS → TEST `shifter/shifter_platform/tests/cms/test_range_model_sources_denial.py` (Canonical deny and policy-dependency outage responses)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_cancelled_launch_cleanup.py` (Absent reservations, verified cleanup, recovered generations and durable capacity release)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_spare_claim_cancellation.py` (Actual PostgreSQL cancellation, spare-claim and owner-transfer lock contention)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_spare_owner_drop_migration.py` (Pre-drop upgrade blocker preserves retained spare custody)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_organizer_direct_read_authority.py` (Native organizer deny and dependency failure behavior)
- TESTS → TEST `shifter/shifter_platform/frontend/e2e/journeys/workspace-lifecycle.spec.ts` (Native workspace lifecycle against released OpenFGA and PostgreSQL)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_session_release.py` (Native service early release denial and due-time session contract)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_organizer_scenario_scope.py` (Explicit scoped replacement catalog discovery and bounded failures)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_recovery_verified_old_generation.py` (Verified native teardown before replacement repointing and crash recovery)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_retained_inactive_handoff.py` (Exact retained handoff settlement for inactive human and service recipients)
- TESTS → TEST `shifter/shifter_platform/tests/engine/services/test_native_warm_retirement.py` (Native service-owned warm retirement, dispatch interruption, custody and verified capacity release)
- TESTS → TEST `shifter/shifter_platform/tests/engine/services/test_native_warm_retirement_postgres.py` (Real warm result-application and retirement lock contention)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/authority_plan.py` (Complete delegation admission and immutable versioned event authority effects)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/migrations/0076_event_authority_effect_plan.py` (Legacy pending-transition blocker and database immutability guards)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/launch_target.py` (Shared target-before-intent lock ordering)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/services/_warm_pool_admission.py` (Atomic global and bucket reservation with capacity draw)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/services/_warm_pool_sweep.py` (Bounded retained-obligation recovery after policy removal)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/services/_range_cleanup_retry.py` (Exact-origin retry of unverified native teardown)
- TESTS → TEST `shifter/shifter_platform/tests/config/test_event_creation_policy_admission.py` (Mounted event creation and unavailable-provider response)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_authority_plan.py` (Complete delegation, immutable plan, HTTP pending receipt and retry)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_authority_plan_migration.py` (Pending legacy upgrade refusal and confirmed history preservation)
- TESTS → TEST `shifter/shifter_platform/tests/engine/services/test_warm_reservation.py` (Limit admission and all-or-nothing capacity draws)
- TESTS → TEST `shifter/shifter_platform/tests/engine/services/test_warm_reservation_postgres.py` (Concurrent global and bucket admission at limit one)
- TESTS → TEST `shifter/shifter_platform/tests/engine/test_native_launch_cancellation_postgres.py` (Native launch/cancel lock contention and post-admission credential revocation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/config/model_access_operation_policy.py` (Explicit parked-feature unavailable classification for all mounted sharing routes)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/model_access/availability.py` (Mandatory parked sharing boundary before membership reads and effects)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event_projection.py` (Current native event-read or eligible participant admission for notification and scoreboard projections)
- TESTS → TEST `shifter/shifter_platform/tests/config/test_native_model_access.py` (Mounted sharing closure for legacy flags and native credentials)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_native_notification_authority.py` (Native notification and restricted scoreboard admission without legacy role bypass)
- TESTS → TEST `shifter/shifter_platform/tests/integration/asgi/test_notifications_ws.py` (Composed WebSocket credential binding, policy changes and replay delivery)
- TESTS → TEST `shifter/shifter_platform/tests/integration/asgi/test_native_ctf_notifications.py` (Real middleware, channel layer, proof expiry and per-topic delivery/replay admission)
