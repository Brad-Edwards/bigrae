# Integrated identity cutover preflight (#2321)

Issue #2321 is the S8 activation boundary defined by ADR-066. This preflight
reconstructs the October 2026 assessment against `dev` at `a80fbc061` and
incorporates the owner's current delivery constraints. It is architecture
guidance, not implementation or deployment evidence.

The supplied issue remains the acceptance contract. The owner approved the
accompanying proposal and its custom integration source work on 2026-10-25,
using one branch and one pull request. This preflight supplies the boundary
constraints for that implementation; it is not deployment or live-acceptance
evidence.

For PLAT-2011, S8 activates the prepared hierarchy and principal contracts;
it does not redesign them. Existing traceability identifies incumbents, not
proof that their production composition or final data constraints are ready.

## Confirmed starting state

- S1-S7 prepared the new account, principal, credential, OpenFGA, GCP identity,
  CTF, content and range contracts, but the production composition still uses
  the incumbent authority paths.
- `openapi/v1.json` contains 252 HTTP operations. The prepared account-
  administration artifact contains 113 operations, including colliding
  replacements that are not mounted by the production URL configuration.
- Prepared account administration is still documented as not linked into the
  deployed portal. `resolve_personal_workspace`, `OPENFGA_ENABLED`, legacy
  session admission, role/staff/owner checks and user-versus-credential service
  branches remain reachable.
- `workspaces.identity_mapping.plan_legacy_account_mapping` is a classifier,
  not an executor. It deliberately blocks ambiguous personal/shared structure,
  missing principal evidence and absent account-type/default facts.
- SQL owns principals, account hierarchy, resource ancestry and operational
  state. OpenFGA is intended to own application permission relationships after
  S8. They cannot participate in one atomic transaction.
- The accompanying proposal assumes no live deployment is available. Repository
  inspection cannot establish deployment availability. Clean-install and
  legacy-data fixtures cannot establish live-cloud qualification or waive the
  supplied issue's live acceptance criteria.

## Binding design constraints

1. One application authorization path becomes authoritative for HTTP, browser,
   Django admin, direct services, commands, queues, callbacks and continuing
   connections. Missing or unavailable policy is a denied/unavailable effect,
   never a legacy fallback.
2. Identity uses durable internal principal IDs and exact immutable provider
   bindings. Email, display name, provider group and Django flags are not
   identity keys or implicit grants.
3. SQL remains authoritative for entity hierarchy, lifecycle, ancestry,
   filtering and search. OpenFGA stores application permission relationships;
   it does not become a duplicate entity database.
4. Every retained legacy fact has a deterministic disposition. Ambiguous facts
   block conversion with bounded identifiers and reason codes; conversion never
   guesses, deletes, silently reassigns or fabricates authority.
5. Schema expansion, data/policy conversion and constraint tightening are
   separate phases. A durable cutover state and the existing authorization
   journal bridge the non-atomic SQL/OpenFGA boundary. Runtime starts only from
   the terminal verified state.
6. Fresh installations create only post-cutover state. The legacy upgrade path
   is issue-specific and uses the existing migration, service and deployment
   seams; it is not a generic migration or rollback framework.
7. Existing sessions and legacy credentials do not cross the authority change.
   They are invalidated or marked for authorized reissue; replacement secrets
   are never synthesized by migration.
8. The change stays on issue #2321 and branch `2321-identity-hard-cut`. One PR
   carries the atomic activation. Ordered commits and test partitions make the
   review tractable without creating a second branch or issue.

## Reference-pattern assessment

The design follows the relevant official patterns:

- OpenFGA recommends domain-specific resource types and relations rather than a
  generic meta-model, keeping application hierarchy in the application data
  store and fine-grained permissions in OpenFGA. It also recommends pinning the
  immutable authorization-model ID in production requests.
- Django recommends separate schema and data migrations, historical model state
  for `RunPython`, and an expand/populate/constrain sequence for new non-null or
  unique facts. It warns about allowing concurrent writes during population.
- Google Identity Platform exposes an immutable `localId` while email can be
  changed. Google IAM recommends immutable federation attributes, dedicated
  workload identities and keyless federation instead of user-managed service
  account keys.
- OWASP recommends deny-by-default authorization and permission validation on
  every request, with enforcement close to the protected operation.
- Kubernetes documents that an HPA will overwrite manual replica changes and
  that suspending a CronJob does not stop Jobs already started. Any legacy
  upgrade procedure must therefore control autoscalers and wait for existing
  effects, not merely scale the portal Deployment.

There is one deliberate deviation: OpenFGA recommends shadow or progressive
rollout for critical migrations. ADR-066 prohibits mixed runtime authority.
S8 uses offline differential fixtures and full integration tests for comparison;
these do not replace the issue's live GCP acceptance evidence.

The detailed integration, verification plan, risk register and official source
links are in
[the S8 proposal](../design/identity-cutover-2321-proposal.md).

## Canonical boundaries and validation layers

Paths below are relative to `shifter/shifter_platform/` unless rooted at
`scripts/`, `platform/`, `.github/` or `shifter/installation/`. These are reuse
boundaries, not claims that the prepared paths are already production authority.

| Layer | Canonical incumbents and required boundary |
| --- | --- |
| Input and wire contracts | `shared.api.closed_serializer.ClosedSerializer`, `shared.api.strict_json`, owning DRF serializers/forms, `shared.api.schema` and `shared.api.contract`. Reject duplicate/unknown keys, malformed IDs, unregistered actions/scopes and oversized inputs. Edge shape checks do not replace service-owned business invariants. Any operator-facts input must be closed and bounded, contain mapping facts rather than executable SQL/policy instructions, and reuse the canonical identity/scope types. |
| Identity and credential admission | `management.services`/`principals`, `shared.verified_identity`, `shared.principal_port`, `shared.credentials.CredentialContext`, `config.service_identity`, `config.session_credentials`, `shared.api.principals` and `shared.api_tokens`. Human proof stays with the existing provider/Django session path, personal proof with Knox, service proof with native Google verification. Preserve exact issuer/subject/audience, activity, expiry, revocation, bearer-first failure and session CSRF. A credential-reference UUID is never proof. |
| Scope, policy and delegation | `shared.identity_scope.ResourceScope`, `shared.authorization` catalog/contracts/model/port, `workspaces.services.resolve_resource_scope`, `_authorization_delegation` and `config.openfga_authorization`. Resolve real SQL ancestry and a complete bounded descendant set before evaluating the registered action and immutable ceiling. Preserve exact store/model pinning, higher-consistency reads, explicit denies and evaluator-error distinction. SQL hierarchy is not a second permission store; provider graph traversal is not authoritative ancestry. |
| Owning services and retained surfaces | Prepared `config.api_account_administration`, domain `policy_views`/`policy_admin`, `shared.admin_policy`, CTF `unified_authorization`, CMS `_unified_authorization` and Engine authority services. Adapters pass the same credential/action/target into services. Stock admin actions/inlines/bulk writes must enter those services or close once an equivalent supported path exists. Preserve collection filtering before count, pagination, search, export and download; provider failure cannot become an empty successful collection. |
| Domain safety and participation | `ctf.services.principal_participation`, participant/team/submission/hint/scoring services, `communication.admission`, CMS/Engine scope projections, quota and model-access services. Human/service administration parity removes owner-only/human-only permission vetoes, not competition eligibility, password-change confinement, lifecycle, capacity, quota, source admission, SSRF/DNS-rebinding or communication audience/link validation. Administrator authority never enrolls a participant. |
| Persistence and uncertain outcomes | Owning Django models/migrations, named constraints, `transaction.atomic`, row-lock order, `AuthorizationOperation`/`AuthorizationMutationFence`, `_authorization_journal` and `_authorization_policy`. Reuse actor/scope idempotency namespaces, intent digests, leases, generations, pinned provider identity and readback/reconciliation. Preserve owner-before-token locking for revoke/rotate/lifecycle. No provider I/O inside SQL critical sections, no success on an uncertain write, no duplicate migration/retry framework. Historical `RunPython` code uses migration-state models, not today's services. |
| Work, callbacks and continuing access | `shared.operation_authority`, `operation_envelope`, `operation_intent`, message envelopes, existing outbox/inbox and domain claims; Engine `_launch_authority`, preparation/plugin/device authority; CTF scheduler/communications; `config.websocket_auth`/`socket_authority` and Mission Control Guacamole services. Preserve original initiator, non-secret credential reference, ceiling, scope, version/digest and attempt/generation separately from executing workload identity. Reauthorize dispatch/retry; authenticate callbacks and exact operation binding. A parsed message or queue possession grants nothing. |
| Secrets and host exposure | `cms.credential_encryption`, `shared.field_encryption`, `shared.credential_delivery`, `shared.cloud.owned_secrets`, mounted OpenFGA files and `scripts/gcp/sync_openfga_secrets.py`. Authorize before secret resolution. Keep payloads out of argv, shell expansion/history, URLs, ConfigMaps, rendered manifests, Terraform state/outputs, browser storage/cache, queue payloads and evidence. Reuse captured subprocess I/O and stdin publication; protect temporary facts files and clean them up. `safe_log` escapes control characters; it does not make a secret safe to log. |
| Configuration and deployment shapes | `config._credential_settings`, `_openfga_settings`, `_env_manifest.py`/`env-manifest.json`; `shifter/installation/loader.py`, `schema.py`, `settings_gcp.py`, runtime inventories; `shared.gcp_native_identity.validate_native_identity`. Carry bounded settings, secret references and matching workload/database identities through every validator. Removing `OPENFGA_ENABLED` requires reconciling deployment enablement/readback shapes too; no process-local environment default may retain a legacy authority branch. |
| Cloud, SQL and runtime admission | GCP Terraform IAM/Cloud SQL modules, `shared.gcp_native_identity.database_duties`, chart `openfga.yaml`/`openfga-duties.sql`, KSA/GSA bindings and native proxy/driver seams. Preserve separate runtime, policy-admin and migrator SQL duties; application grants do not grant IAM. Retain deliberately assigned operator powers and narrow workload defaults. Runtime tuple writers remain trusted: preshared authentication and client method allowlists do not establish per-client store/model privileges. Check admission-policy container/env/volume allowlists, Pod security, RBAC, private DNS/API/metadata/SQL egress and loopback listeners against the actual rendered Job/Pod. Do not broaden guest/plugin metadata or network access to fix a control-plane connection. |
| Errors, audit and observability | Owning domain exceptions, `shared.errors`, `shared.api.errors`, `shared.audit`/`audit_adapter`, request IDs, `config._logging_config` and `shared.log_sanitize`. Reuse authored opaque error codes and the request-ID envelope; map plain `PermissionError`/provider errors at the existing adapter boundary rather than leaking an unhandled 500 or adding an exception tree. Strict mutation audit commits with intent; uncertain provider/audit outcomes remain blocked. Logs/metrics use bounded reasons and safe IDs, never claims, tuples, credentials, provider bodies or unbounded identity labels. |
| Clients and contract coverage | `frontend/src/api/client.ts`, generated `schema.d.ts`/`account-admin-schema`, existing Query mutations, `shifter/shifter_client.ServiceClient.from_adc`, versioned OpenAPI and retirement metadata. Keep same-origin CSRF, disabled mutation retries and one-time secret delivery outside caches. The supported service client uses separate auto-refreshed application/cloud transports and rejects redirects; do not add raw-token suppliers, custom renewal or handwritten duplicate DTOs. |

## PLAT-2011 hierarchy and scope activation

- **Default hierarchy invariants.** Reuse `_account` through
  `workspaces.services` and the policy-aware `_account_policy_admin` commands.
  Individual accounts have no subdivisions. Team/enterprise accounts need
  exactly one default organization; every organization retained in that business
  hierarchy needs one active default workspace, including additional
  organizations. The partial unique indexes prove *at most one*, not existence
  or correct account type.
  Domain locks and final mapping verification must prove the remaining facts.
  Default creation must create neither membership/ownership rows nor policy
  grants; it cannot repair ambiguous history by manufacturing defaults.
- **Membership facts and grants.** Existing organization
  and workspace rows remain intact before S8. Their current identity constraints
  permit either a legacy `user` with a role or a principal-only fact with an
  empty role; setting both identities fails the database check. S8 needs an
  explicit disposition for each durable row and any overlapping prepared fact,
  preserving row identities where valid rather than delete/recreate or duplicate
  insertion. Membership, `WorkspaceOwnership`, range ownership, creator and
  OpenFGA grants remain distinct. Preserve multiple-owner and last-owner
  invariants; never turn every member into an owner or infer an account grant
  from the personal-account principal field. Resolve active principals through
  `management.services`/`shared.principal_port`, including independent services.
- **Scope tightening is conditional.** `ResourceScope` owns shape;
  `resolve_resource_scope`/`resource_scope_from_ids` own SQL ancestry and
  lifecycle; owning models own scalar columns and named database constraints.
  Existing resource scalar scope and owner identities remain unchanged until
  the coordinated S8 conversion.
  The current CMS/Engine/CTF checks admit the transitional empty `scope_kind`
  branch. Final enforcement removes that branch only after retained rows have
  explicit dispositions. Require `account_id` for account scope, not globally:
  explicit installation scope has no customer IDs, and each resource's owning
  service determines whether it permits installation placement at all. Optional
  workspace never means unknown customer or installation scope. Cross-row
  ancestry cannot be established by scalar shape checks or `Model.clean()`;
  direct/bulk writers must enter the owning command. Preserve internal keys and
  public UUIDs wherever ancestry remains valid; neither labels nor row order
  determines ancestry, account type, defaults or ownership. The existing mapping
  classifier scans only unbound organizations; final verification must also
  inspect already-bound and prepared rows rather than treating them as verified.
- **Optional scope across runtime boundaries.** Account-only
  ranges/events must reach the same parsers, workers and read projections as
  workspace-bound ones. In particular, `shared.warm_pool.activation_input`
  requires a positive workspace ID and human claimant, composed by
  `engine.operation_inputs`; replacing a controller alone cannot support an
  individual/service claimant. Inspect CMS `_warm_pool_claim`, `_range_workspace`,
  `_range_reassign`, model-source selection and range-event/quota handlers;
  Engine `_range_backend_binding` and `_sharing_ranges`; CTF public registration,
  event model sources and communication admission/release. Replace obsolete
  assumptions at their canonical contracts and both producer/consumer ends;
  preserve purpose, generation, quota, model-access and participation checks.
  Do not invent a placeholder workspace/user or turn a missing value into a
  wildcard to pass a downstream validator.
- **Descendant proofs must include direct account resources.** The incumbent
  `shared.authorization.inventory` and Engine `_authorization_inventory` take
  workspace IDs; the separate CTF non-workspace event resolver already handles
  account/organization placement. A direct account range can otherwise disappear
  from delegation and explicit-deny proofs. Extend the existing SQL-owner
  inventory seam to accept the canonical parent scope and return exact typed
  target/scope facts, retaining completeness bounds and fail-closed overflow.
  Do not discover ancestry through OpenFGA or create a second resource registry.
- **Reuse S4/S6 writers at the live composition.** Profile changes reuse
  `_policy_profiles` and `_organization`'s PATCH mask/validation; workspace
  lifecycle reuses `_account_policy_lifecycle` and `_lifecycle`'s locked writers,
  including default-archive rejection and model-access invalidation. CMS
  `_scoped_range_scope`, Engine `_principal_range_scope`, and
  `shared.range_workspace_aggregate` already preserve source/destination checks,
  generation fences and aggregate custody; do not extend the older
  `_policy_range_scope`'s human/workspace assumptions as a parallel path.
  Wire the prepared adapters at `config.urls`/`api_urls`, invitation composition
  and the existing admin site. Prepared admin stays read-only; supported writes
  enter canonical commands. Listing, counts and UI context must work without a
  workspace and must remain filtered by live policy before pagination.

Extend the existing `tests/workspaces/test_account_hierarchy.py`,
`test_legacy_account_mapping.py`, `test_policy_memberships.py`,
`test_policy_profiles.py`, `test_policy_workspace_lifecycle.py` and
`tests/cms/test_account_scope_schema.py` patterns. Final migration-state tests
must reject legacy shapes and conflicting mappings while preserving valid IDs;
integrated direct-account cases must exercise the actual operation parser and
SQL/OpenFGA descendant proofs, not just mocked controller responses.

## PLAT-232 organization profile activation

The [#1939 record](organization-profile-settings-preflight-1939.md) describes
the retained S1 surface, not S8 authority. Before activation its session-only
org-admin/superuser rules remain intact. At S8, `organization.read` and
`organization.update` use the shared principal policy and credential ceiling;
neither the legacy role nor the Django override remains an alternative grant
or an additional veto. Explicit historical admin facts need a deliberate
policy-mapping disposition, not a grant inferred from ordinary membership,
creator, default status, staff, provider claims or cloud IAM. Default creation
still grants nothing. Individuals have account settings, not an organization.

The following are activation obligations, not claims that preparation already
satisfies them. Paths use the relative roots defined above.

| Layer | Canonical reuse and profile-specific guardrail |
| --- | --- |
| Identity and HTTP admission | `shared.api.principals.authenticated_credential`, `config.session_credentials`, bearer-first authentication and `shared.credentials` own proof and lifecycle. The prepared `_AccountAPIView` admits human/service credentials; preserve browser CSRF and temporary-participant exclusion. Do not retain S1 token rejection as a second S8 gate or admit the legacy unreferenced-session fallback as durable proof. Body fields never supply actor, credential or ceiling. |
| Scope and policy | `hierarchy_target_scope`/`resolve_resource_scope`, `TargetRef`, `AuthorizationRequest` and `shared.authorization.policy` own exact SQL ancestry, registered action, ceiling and evaluator outcome. `_policy_profiles` already checks the path account against the organization's real parent. Keep team/enterprise ancestry mandatory; changing the selected account cannot reparent a profile. Read permission does not imply update, membership or executable-adapter authority. |
| Parsing and validation | Reuse `ClosedJSONParser`, `ClosedSerializer`, `OrganizationProfileUpdateSerializer` and `_organization._validate_changes`; no parallel profile DTO or validator. The existing profile serializer rejects unknown keys in `validate()`; converge it on the closed serializer before publication so hostile key names are not reflected in error-detail keys. Require a bounded JSON object, reject duplicate/unknown keys, nulls, identity/ancestry/default fields and invalid types/formats/lengths. Keep service validation for direct callers. The four fields remain `name`, `description`, `support_email`, `support_url`; absent means unchanged and empty string clears an optional field. |
| SQL commit and revocation | Reuse `_policy_profiles`, `_policy_memberships._locked_scope`, the organization mutex, `transaction.atomic` and named constraints. The prepared writer checks policy before locking but does not call `resolve_current_credential` or `resolve_local_credential`. Reuse full live credential/policy admission before locks and the incumbent local credential/ancestry checks at commit, as in Engine `_principal_range_create`; preserve owner-before-token lock ordering. A credential reference is not proof. No provider I/O under SQL locks. An organization mutex cannot serialize an OpenFGA revocation: define and test the ordering of an admitted write racing a policy revoke without claiming cross-store atomicity or creating a second journal. |
| Errors and availability | `shared.authorization.policy.require_authorization`/`authorized_requests` preserve evaluator failure separately from denial; `AuthorizationProviderBindingError`, `_AccountAPIView`, `shared.api.errors` and `ApiErrorSerializer` supply the existing mapping/envelope. The prepared `_require_action` and `_allowed_batch` currently collapse failures into `AccountScopeError`; correct that at the common seam rather than in a profile-only exception tree. Missing, wrong-parent and forbidden UUIDs stay opaque; evaluator outage yields sanitized unavailable behavior, never an empty successful list or a misleading policy denial. Map service validation and permission errors once; expose no raw exception, integer key, submitted value or provider response. |
| Audit and observability | Reuse `_organization._write_audit`, `request_audit`, principal attribution and `shared.audit` strict writes. A real mutation and its single field-name-only event commit together; audit failure rolls back, and no-op PATCH emits no mutation event. Keep principal UUID attribution and frozen historical hashes; do not label policy admission as a Django override. `RequestIDMiddleware`, `shared.log_sanitize` and `config._logging_config` own correlation and sanitization. Exclude profile values, credentials, claims and raw bodies; sanitization alone does not remove sensitive data. The internal integer audit target remains internal and does not authorize customer audit reads. |
| API and client contracts | Reuse `OrganizationProfile`/`OrganizationProfileSerializer`, `_policy_profiles` and `PolicyOrganizationProfileView`; serializers generate both OpenAPI artifacts and their TypeScript projections. Production currently mounts `/api/v1/workspaces/organizations/{uuid}/`, while preparation uses `/api/v1/accounts/{account_uuid}/organizations/{organization_uuid}/`. Give every retained or retired path, operation ID and old deep link an explicit disposition under `shared.api.contract` and existing breaking/retirement checks. Any retained adapter must reach the same policy command; leaving the old route live preserves legacy authority. Organization identity stays UUID-only in all projections, nested links, errors and client state. |
| Browser and discovery | Reuse `apiFetch`, TanStack Query, `ApiError`, shared form/alert controls, `router.tsx`, `RootLayout.RouteGate`, `app/nav.ts` and account context. The current staff gate and `OrganizationConsoleLayout`'s workspace-context requirement hide settings from valid non-staff administrators with no workspace membership. Prepared account/organization lists filter `read`, whereas the old chooser lists administrable profiles: do not silently equate readable with editable. Preserve separate advisory affordances, policy filtering before counts/pages and access to every page; no account/workspace membership requirement substitutes for the leaf decision. Consolidate the existing and prepared profile forms/hooks rather than maintain two field contracts or stale cache families. |
| Configuration, secrets and host exposure | Profile fields are database product data, not env, secret, cloud-label or deployment inputs. This surface adds no setting, secret, worker or subprocess. S8's existing OpenFGA activation still crosses `_openfga_settings`, `OpenFgaRuntimeSettings`, `_env_manifest`/`env-manifest.json`, installation schema/runtime inventory and Helm/GCP render/admission contracts described above. Preserve mounted secret files, pinned store/model, bounded timeouts, TLS and least-privilege workload/SQL duties. Never interpolate a profile value or credential into argv, shell, environment, manifests, guest metadata or evidence. Keep values out of persistent browser storage. `support_url` is validated metadata; do not fetch it server-side or render profile text as HTML. |

**Cross-domain trap.** `get_organization_profile` is currently reused as an
administrator guard by CMS `_tenant_pack_upload`, `_tenant_pack_removal`,
`_content_ingestion`, `_pack_conformance`, `_runtime_plugin_packs`, scenario
metadata and `cms.api.tenant_packs`; Engine `_runtime_plugins` and
`_model_sources` also consume it. Workspaces lifecycle and model-access code
use `resolve_administrable_organization`. Replacing those checks with
`organization.read`, or deleting the helper after changing only its HTTP view,
either widens writes or strands legacy authority. Each retained effect must
use its registered action through the prepared CMS/Engine/workspaces authority
seams (`_content_authority`, `_runtime_plugin_authority` and policy commands),
with its existing pack validation, installation binding, encryption, quota,
model-access and isolated-worker boundaries intact. Profile edits grant none
of these effects. Preserve the per-symbol service facade allowlists in
`.importlinter` and `scripts/check_layer_imports/layer_imports.yaml`; do not
expose private services or ORM models to remove a caller error.

**Extension seam.** Parameterize the existing command by admitted
`CredentialContext`, resolved account/organization UUIDs and the closed PATCH
mask. Parameterize the shared form by the selected UUID scope and canonical
query/mutation hooks; reset drafts on target change and invalidate profile,
chooser and account-list names together after renaming. A future approved field
extends the existing projection/serializer/service validation once. No generic
settings JSON, duplicated `display_name`, browser role matrix or policy cache.
`updated_at` is a possible future precondition seam; current PATCH masks prevent
unrelated-field overwrite but do not detect same-field conflicts.

**Required evidence.** Extend the existing `test_organization`,
`test_organization_api`, `test_policy_profiles`, `test_prepared_admin_routes`,
released `test_openfga_prepared_administration`, credential-revocation and
frontend organization/account suites. Verify actual production resolver and
browser composition, not only the dormant URLConf or allow-all provider.
Cover read-only versus update authority, human/service parity, non-staff/no-
workspace access, sibling/wrong-parent denial, temporary confinement, all pages,
hostile JSON keys, audit rollback/no-op, credential/policy revocation races,
provider failure and surviving cross-domain callers. Use real PostgreSQL for
locks/constraints and the existing released OpenFGA lane for policy; schema/type
drift, breaking-change, layer/FK, accessibility and ADR gates remain mandatory.

**Boundary.** This is PLAT-232 guidance for the coordinated S8 issue, not an
independent cutover or authority to implement C6. No organization-profile create,
delete, reparent, default/grant mutation, branding pipeline, deployment-setting
inheritance, customer audit reader, new authentication/policy/error/workflow
framework or extra infrastructure belongs in the profile change. Broader S8
effects retain the owning-domain boundaries already recorded in this note.

## Integration traps that must remain visible

- **Artifact coverage is not effect coverage.** The verified 252 and 113 counts
  describe committed schema artifacts, not a count of mounted operations. Reuse
  `shared.authorization.operations.OperationPolicy`, the CMS/CTF/Mission
  Control/Engine `operation_policy.py` inventories and existing surface-coverage
  tests. Reconcile actual URL-resolved method/path/operation IDs with the
  historical 220, every addition and explicit retirement. Prepared routes can
  collide with earlier includes. Inventory HTML, admin fields/actions/inlines,
  commands, direct services, queue handlers, callbacks, communications, sockets
  and data-plane access independently; metadata entries do not enforce policy.
  New surfaces need an action/SQL locator or a narrowly justified exemption.
- **Prepared human assumptions still reject services.** `shared.admin_policy`
  deliberately admits only browser sessions; service parity belongs in the
  canonical API/services, not a fabricated admin login. CTF
  `communication.admission.AdmissionActor` still uses user/token/system fields
  and workspace-only target checks. `config.socket_authority` can prefer a
  browser `User`; `shared.api.principals` can manufacture an unreferenced legacy
  session context. These paths need deliberate S8 dispositions, not another
  credential-to-User adapter. Personal proof already delegates to Knox in
  `ApiToken.authenticate`; retire obsolete verifier metadata without replacing
  Knox or retaining a compatibility verifier.
- **Mapping includes all retained evidence.** Build on
  `workspaces.identity_mapping.plan_legacy_account_mapping` and owning-domain
  projections for provider bindings, hierarchy/defaults, memberships, groups,
  roles, CTF staff/owners/participants, resources, tokens and pending operations.
  Preserve durable IDs, revoked/archived states and explicit denies. A changed
  source snapshot invalidates its mapping evidence. Missing initiator or
  credential evidence blocks pending work; never substitute its owner or a
  system actor. Reuse `shared.api_tokens.scopes.credential_ceiling` for scope
  conversion; unmapped legacy scopes cannot silently become broader actions.
  Verify unintended/stale parent or grant edges are absent as
  well as required edges present. Archiving a synthetic subdivision cannot
  cascade-delete retained resources. Historical audit hashes/actors remain
  unchanged; reconciliation evidence is appended through the existing ledger.
  Historical audit without trustworthy customer scope remains installation-only.
- **Readiness must not deadlock maintenance.** A terminal-state requirement
  cannot query absent tables or require live policy from Django imports or
  `AppConfig.ready`, preventing migrations, schema generation or first bootstrap.
  Keep effect-producing process admission separate from narrowly identified
  deployment-owned maintenance commands. First-grant bootstrap has no incumbent
  caller grant: the custom integration source approved for #2321 must keep
  exclusive execution, exact active provider binding, strict durable
  intent/audit and pinned-model readback. Neither an empty SQL database nor a
  missing setting proves that an existing policy store is clean or authorizes a
  root grant.
- **Quiescence covers more than the portal.** The canonical path is
  `.github/workflows/_gcp-dev.yml`, `shifter/installation/gcp_migrations.py`,
  `scripts/gcp/render_migration_job.py`, `render_runtime_env.py`,
  `render_native_identity.py`, `render_overlay_identity.py`, the Helm chart and
  GCP Kustomize overlays/base. The migration Job currently runs DB-oriented
  commands without policy mounts; it cannot simply inherit a new networked
  converter. Reconcile prerequisites, migration credentials and egress through
  these incumbents. Stop/drain all workers, schedulers, outbox/result appliers,
  reconcilers, ephemeral provisioner/plugin Jobs and continuing-access gateways;
  account for HPA, CronJobs and already-started effects. Gateways, sessions,
  cached admission, existing tunnels and issued access material need explicit
  invalidation or tested finite termination bounds, including idle access.
- **Revocation must not abandon effects or cleanup.** Persist original authority
  through the existing operation contracts and intersect its ceiling with live
  authority on replay. Preserve versioned digests and reconciliation after
  uncertain external outcomes. Authentic results of already-started work remain
  recordable after revocation. Required cancellation/cleanup uses only the
  existing explicit bounded system purpose, exact resource/generation and
  workload proof; it cannot launch, publish, rehome or create grants. No
  missing-actor fallback, audit `SYSTEM` label or global bypass flag.

## Extension seams and evidence limits

New actions belong once in `shared.authorization` and the owning operation
inventory; new resource variants extend its typed target/SQL-resolver seam.
Provider adapters terminate at the existing `CredentialContext`, not domain
schemas. Parameterize audience, store/model IDs, project/resource references
and bounded lifetimes/recheck intervals in the existing closed configuration
and render paths. Keep backend and workload-duty selection in the existing
installation registry/native readback contract. A new workload must join that
inventory and its admission/network/SQL checks; it must not require copying
identity logic or editing each authenticator. No extensible migration DSL is
justified by this one-time conversion.

Evidence must cross the real composition: resolver coverage, canonical API
contract/type drift and breaking-change checks, direct-service and browser
checks, real PostgreSQL constraints/races, and
`shifter/shifter_platform/scripts/run_openfga_integration.sh`'s pinned
released-server lane. Reuse the
existing credential-rotation, authorization, CTF, operation and continuing-access
tests. Exercise empty installs and retained-data fixtures, conflicting mappings,
strict-audit failure, uncertain writes/reruns, revocation, sibling-account
isolation, temporary confinement, human/service administrator parity and
explicit service participation under competition rules. Test rendered
Helm/Kustomize/admission contracts as well as workflow maintenance behavior.
Retain `.github/workflows/_quality.yml`, `.importlinter`, `.ground-control.yaml`,
ADR registry/exceptions and the relevant stack-native gates; do not weaken a
validator or classify a skipped required integration as success.

Local expiry/refresh fixtures cannot establish sustained native renewal, SQL
reconnection, effective cloud IAM, live CTF access or actual data-plane
termination. The owner confirmed that no live deployment exists, so record
those as deferred first-deployment qualification instead of claiming live
acceptance in this delivery. Later delivery records reconcile the issue, ADR,
documentation and supplied PLAT-2011 traceability without treating repository
tests as live-cloud evidence. No requirement status or traceability is changed
by this preflight.

## Non-goals and prohibited shortcuts

This preflight changes guidance only: no application source, dependency, data,
policy, credential, cloud resource, deployment, requirement or remote metadata.
It authorizes no C6 implementation, live test, service/data transfer or merge.
AWS #2313 remains separate. Private scenario content and acceptance evidence
remain in their owning repositories; use synthetic fixtures here.

Do not add a policy engine, role table, second scope schema/validator, exception
hierarchy, token format, secret broker, renewal loop, queue/cleanup framework or
generic migration/rollback framework. Do not combine old/new evaluators with
OR/fallback/shadow behavior, infer grants from membership/ownership/provider
claims/cloud IAM, fabricate workspaces or human users, silently drop ambiguous
rows, or waive lifecycle/competition/secret/admission checks for administrators.
No staged compatibility window or deployment rollback machinery; ordinary SQL
transaction rollback remains required database behavior.
