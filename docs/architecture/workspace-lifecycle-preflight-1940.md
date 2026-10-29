# Workspace lifecycle preflight (PLAT-233, #1940)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.


Status: pre-implementation guidance reconciled with ADR-066 and #2314;
production authority cutover remains the coordinated S8 change

Date: 2026-09-20

Requirements: PLAT-233 (implements), PLAT-241 (constrains)

This note fixes the repository-wide boundaries for workspace create,
list/search, rename, archive/restore, and ownership transfer. It adds no
application behavior and is not an implementation plan.

## S8 activation boundary (#2321)

Reviewed: 2026-10-25. This section supersedes the historical S1 guidance below
where that guidance requires session-only admission, persisted roles, an
organization/superuser override, owner-only transfer, or a future pagination
design. S1 retains the legacy APIs and SPA under their existing authority;
S4 prepares replacements; only S8 activates principal policy across all retained
surfaces. Neither traceability nor prepared-route tests prove activation.
Read this with [ADR-066](../adr/066-account-principal-resource-scope.md), the
[S4 operation inventory](account-administration-coverage-2317.md), and the
[integrated S8 preflight](integrated-identity-cutover-preflight-2321.md).

**Authority and reuse.** `workspaces.services` remains the owning boundary;
`shared` owns neutral contracts and `config` composes identity/provider ports.
Use the existing commands, not another lifecycle controller or role evaluator:

| Effect | Prepared incumbent and registered action |
| --- | --- |
| List/search/detail | `_account_policy_reads`, `WorkspaceAdminFilters` and its bounded offset/limit pages; `workspace.read` on each exact SQL-owned target before count/page. Creation or parent-read authority alone does not authorize discovery. |
| Additional workspace | `_account_policy_admin.admin_create_workspace` -> `_account.create_account_workspace`; `organization.manage_workspaces` on the resolved parent. Structural default creation stays in the account-locked hierarchy service. |
| Rename/archive/restore | `_account_policy_lifecycle` -> `_lifecycle` locked writers; `workspace.update`, `workspace.archive`, `workspace.restore`. Keep existing name validation, audit, no-op and model-access invalidation behavior. |
| Ownership | `_policy_memberships.admin_transfer_workspace_principal` and owner commands; `workspace.transfer`, explicit principal membership and `WorkspaceOwnership` facts. Integer-user compatibility delegates through `shared.principal_port`; it must not edit legacy roles. |

Membership, ownership, policy grants, creator attribution and structural default
status are separate facts. Full human/service administrators need no owner role,
Django staff flag or dummy human to pass application policy. Ownership transfer
changes facts, not grants: the destination is an active explicit workspace
member; an expected source must still own under the workspace mutex; a null
source is valid only while no ownership exists. Additional owners are retained,
and last-owner protection applies once an owner exists. Initial membership,
initial ownership and any policy assignment each need their own authorized
command. Creation alone performs none of them; transferring ownership must not
silently give the destination application permissions or revoke the source's.

**Preparation gaps that activation must close.**

- `_account_policy_admin._require_action` validates an active principal but
  does not revalidate the admitted credential. Reuse
  `shared.credentials.resolve_current_credential` and live policy before SQL
  locks, then `resolve_local_credential` and exact persisted account/organization/
  workspace and lifecycle checks at commit. Engine `_principal_range_create`
  demonstrates this separation; consume shared seams, not Engine internals.
  `_account_policy_lifecycle._locked_workspace` currently checks organization
  identity only, unlike `_policy_memberships._locked_scope`'s full ancestry
  check. Keep credential owner-before-token and hierarchy lock ordering
  consistent with existing writers. No provider I/O under database locks;
  a workspace lock cannot make SQL and OpenFGA revocation atomic. Define and
  verify admitted-write/revocation ordering at the existing policy boundary.
- `_require_action` and `_account_policy_reads._allowed_batch` collapse
  evaluator failures into `AccountScopeError`. Reuse the distinctions in
  `shared.authorization.policy.require_authorization`/`authorized_requests`
  at the common seam. Preserve opaque missing/wrong-parent/forbidden denials,
  sanitized unavailable responses for outages/incomplete batches, and existing
  classified validation/conflict outcomes. `_AccountAPIView` currently maps
  lifecycle conflicts such as `name_taken` and `default_workspace` to 403;
  reconcile it with `_WorkspaceAPIError`'s 409 mapping rather than creating
  another exception hierarchy. Classify creation uniqueness races too; never
  expose raw `IntegrityError`, provider errors or submitted values.
- `_ensure_shared_defaults_locked` verifies only the default organization.
  S8 must verify exactly one **active** default in every retained business
  organization, including additional/already-bound organizations. The partial
  unique index proves only at-most-one; it proves neither existence, active
  state nor parent account kind. Resolve missing, archived or ambiguous defaults
  through the established mapping boundary, not a GET-time repair or a name/
  ordering heuristic. Do not mark personal compatibility storage as a default
  of an individual account. Individuals have no subdivisions.
- SQL creation alone does not make the new hierarchy usable by policy.
  `config.openfga_authorization._ancestry_matches` requires persisted exact
  parent relations; `_account` creation does not publish them, and the released
  administration test seeds them manually. Cover post-cutover default/additional
  creation as well as retained rows. Parent relations project SQL ancestry,
  not membership or grants. Reuse the durable authorization journal/provider
  boundary and readback/uncertainty rules; any missing parent-effect support
  belongs in those incumbent closed contracts. No raw request-supplied tuples,
  ad hoc SDK write, second reconciler or claim of SQL/OpenFGA atomicity. A
  missing/ambiguous/pending parent projection cannot broaden access or be
  reported as fully usable policy state.
- `identity_mapping.plan_legacy_account_mapping` is a read-only classifier.
  Membership conversion must satisfy the existing user/principal XOR and
  empty-role constraints and reconcile overlapping prepared rows without losing
  valid durable identities. Legacy owner roles map deliberately to ownership
  facts; retained permission relationships use the existing authorization
  journal under the cutover mapping decision. Filling `principal_uuid` beside
  a legacy user/role is invalid, and deriving new grants from an ownership fact
  after cutover is invalid. Preserve workspace IDs, archival state and range
  bindings; ambiguous evidence blocks conversion.
- `_scope_for_lifecycle(..., restore=False)` rejects an archived row before
  archive reaches the idempotent locked writer. Preserve authorized repeated
  archive/restore as no-ops, with no duplicate mutation audit/invalidation.
  Archived discovery/restore require explicit operation-specific ancestry
  resolution; do not globally loosen `resolve_resource_scope`. Archive never
  deletes/reparents ranges or launches cleanup. Preserve the incumbent
  `_model_access.invalidate_workspace_model_access` call for real transitions;
  archive is not merely an unaudited marker update. New placement remains
  inadmissible there; existing resource access/cleanup uses owning-domain gates.
- `AccountPageQuerySerializer` and its search/workspace subclasses are ordinary
  DRF serializers: bounded fields alone do not reject unknown/repeated query
  keys. Follow the existing `ClosedSerializer` plus singleton-`QueryDict`
  pattern in `ctf.api.organizer.communication.validate_query` without importing
  across domains;
  retain `include_archived`, search <= 200, offset >= 0 and limit 1..100.
  Bodies reuse `ClosedJSONParser`/`ClosedSerializer`, including empty commands.
  Reuse `_lifecycle._validate_name` semantics for every creation/rename entry,
  plus database organization/name uniqueness; do not add a second normalizer
  or silently change case/collation rules. `_visible_page` batches provider
  calls but scans all candidates for an exact count: bound total request cost
  without silently returning a partial count/page as complete.

**Cross-cutting layers that must pass.** Paths here are relative to
`shifter/shifter_platform/` unless explicitly rooted elsewhere.

| Layer | Required incumbent and boundary |
| --- | --- |
| Identity, authentication, CSRF | `shared.api.principals.authenticated_credential`, `shared.credentials`, `shared.principal_port`, `config.session_credentials`/`service_identity`, bearer-first `config._drf_settings`. Preserve provider proof, live principal/credential, exact ceiling and session CSRF; exclude temporary participant credentials. A credential UUID or legacy unreferenced-session fallback is not durable proof. S1 token rejection must not become a second S8 veto. |
| Contract and policy shapes | `PrincipalRef`, `ResourceScope`, `TargetRef`, `AuthorizationRequest`, `ACTION_CATALOG`, `shared.authorization.policy`, and `workspaces.services` ancestry. Validate closed kinds/non-nil UUIDs and actual team/enterprise containment, then exact action/ceiling/live decision. Missing IDs never imply installation. SQL owns hierarchy; OpenFGA owns permission relationships. |
| Parsing and safe errors | Owning DRF commands, closed parser/serializer, bounded query serializers, `shared.errors`, `shared.api.errors` and `ApiErrorSerializer`. Validate shape at HTTP and invariants for direct callers. Map once to the request-ID envelope, preserving 400/409 versus opaque denial/unavailability; no hostile keys, SQL text, roster or provider bodies in details. |
| Persistence and evidence | Existing models/migrations, named constraints, `transaction.atomic`, row mutexes, `_lifecycle._write_audit`, `request_audit` and `shared.audit` strict writes. Mutation and audit commit together; failures roll back. Preserve principal attribution and historical hashes. `RequestIDMiddleware`, `config._logging_config` and `shared.log_sanitize` own correlation; logs use bounded IDs/reasons, metrics only bounded action/outcome labels, never names/searches/credentials or identity labels. |
| Config, secrets, runtime and OS | Lifecycle needs no new env key, secret, worker or subprocess. S8 policy binding still passes `config._openfga_settings`, `OpenFgaRuntimeSettings`, `_env_manifest`/`env-manifest.json`, `shifter/installation/{loader,schema,runtime_inventory*}`, `platform/charts/shifter/values.schema.json`/OpenFGA and portal templates, and GCP runtime/migration renderers. Keep mounted bounded token files, HTTPS/CA verification, exact store/model, bounded timeout and private service/network policy. URL validation alone does not prove private reachability. No tenant value or credential goes into argv, shell text, environment, public Vite config, manifests, Terraform values/outputs, guest metadata or evidence logs. No new service/data transfer is authorized. |
| Browser and publication | `account-administration.ts`, `client.ts`, Query mutations, `AccountAdministrationPage`/`WorkspaceAdministrationPanel`, existing router/navigation and CSP/cookie policy. Use same-origin cookies/CSRF and no automatic mutation retries. Generated `account-admin-schema.d.ts`/`schema.d.ts` follow owning serializers and OpenAPI; no hand-copied DTOs. |
| Repository enforcement | `.importlinter`, `scripts/check_layer_imports/layer_imports.yaml`, `check_model_fks`, ADR registry/exceptions, `.ground-control.yaml`, `.gc/plan-rules.md`, `.pre-commit-config.yaml`, `.github/quality-path-filters.yaml` and `_quality.yml`. Keep owning service facades/scalar cross-layer references and normal architecture, contract, security and stack-native gates. |

**Live composition and extension seam.** `config.account_admin_urls` is dormant;
production uses `config.api_urls` -> `workspaces.api.urls`, and the SPA's
`administer` ancestor still requires staff. S8 must give every old path, method,
operation ID and deep link an explicit retained/replaced/retired disposition
under `shared.api.contract` and the existing retirement checks. Retained
adapters enter the same principal commands. The legacy array list, integer-user
transfer payload, and prepared count/results/UUID contracts are not interchangeable;
regenerate production OpenAPI/security and clients from the mounted result.
No legacy alias may retain a role check, implicit creator grant or personal
fallback. Prepared `PolicyReadOnlyAdmin` authoring stays disabled; inspect actual
admin registration, inlines/bulk actions, service callers and public callbacks.

Browser reachability comes from exact server policy, independently of membership
and ownership facts. Reuse prepared account/org/workspace parameters and
`WorkspaceAdminFilters` rather than inventing another selector, page format or
context DTO. A creator need not have read permission; do not auto-grant it to
make a new workspace appear. Read-only and narrowly delegated operators must
not need roster/policy/quota authority to open lifecycle detail. Membership
pickers must respect pagination; the first 100 members are not the full roster.
Invalidate the existing list/detail/context queries after mutation and on actor
change; cached capabilities and disabled-default buttons never authorize.
Render names as text through existing components. Human/service variation
belongs in `CredentialContext`/`PrincipalRef`; action
variation belongs in the central catalog/model/ceiling mapping. Any future
default-reassignment command must atomically preserve one active default using
the existing hierarchy mutex, not add another default field or writer.

**Evidence and limits.** Reuse `test_account_hierarchy`,
`test_policy_workspace_lifecycle`, `test_policy_memberships`, legacy lifecycle
service/API tests, `test_prepared_admin_routes`, browser account/lifecycle
journeys and the released `test_openfga_prepared_administration` harness.
Activation evidence must use production URL/middleware/browser composition and
real credential/model decisions, including scoped/full human and service admins,
sibling isolation, ownerless creation, archived discovery/replay, default/name/
last-owner races, credential/policy revocation, provider outage, strict-audit
rollback and unchanged bound ranges. PostgreSQL proves locks/constraints;
SQLite and allow-all providers do not. Run `adr_guard --all --level ci`, relevant
import/FK/migration guards, contract drift/breaking checks and selected native
backend/frontend/PostgreSQL/OpenFGA gates. Source checks are not live GCP proof.

This note adds no source, migration, deployment or data mutation and does not
authorize #2308 C6 work. No parallel lifecycle framework, repository layer,
schema, exception tree, policy engine, queue, generic migration/rollback
machinery or mixed-authority window is needed. Default reassignment, workspace deletion,
account-kind conversion, credential UI, range transfer and cloud/IAM changes
remain outside PLAT-233 lifecycle activation.

## Historical S1 readiness decision

PLAT-233 is architecture-ready under ADR-066, but the current lifecycle paths
encode the superseded personal-workspace model and cannot simply be extended:

- `workspaces.services._lifecycle.create_workspace` implicitly grants its
  creator `owner`, while ADR-066 requires hierarchy creation without an inferred
  grant;
- `workspaces.services._account.create_account_workspace` creates the same row
  without that grant, so the two paths currently have divergent validation,
  audit, authorization, and concurrency behavior;
- workspace and organization roles still target Django users, while ADR-066's
  durable membership identity is a principal;
- the SPA lives below a staff-only `/administer` route even though the API
  admits any active session and then applies organization/workspace authority;
  and
- the SPA workspace layout resolves detail from the caller's membership context,
  which cannot resolve a newly created workspace when creation grants no
  membership.

The implementation must converge these paths at the existing
`workspaces.services` boundary. It must not activate account/principal authority
alongside the legacy user/personal authority. Schema and mapping may land before
S8, but runtime authority changes together at S8 after mapping validation passes.

## Fixed domain and lifecycle boundaries

`workspaces` remains the sole persistence and policy owner for Account,
Organization, Workspace, their memberships, and workspace lifecycle. Public
surfaces use immutable UUIDs; internal joins and cross-domain scalar bindings
keep their existing integer IDs. No generic repository, tenancy app, JSON scope
bag, cross-layer foreign key, model signal, or frontend-owned lifecycle state is
introduced.

The lifecycle surface admits only an organization with a non-null Account whose
closed kind is `team` or `enterprise`. An individual account has no organization
or workspace. A legacy row with `Organization.account_id IS NULL` and a personal
compatibility workspace remain S8 mapping input and storage; neither is a
business-account default or eligible for the account lifecycle API.

Every eligible organization has exactly one default workspace and may have
additional workspaces:

- the partial database uniqueness constraint enforces at most one default;
- the account-locked domain service creates and verifies at least one default
  atomically and idempotently;
- `is_default` is the single structural designation and is included in the
  service/API projection so clients never infer it from name or order; and
- until a separately accepted set-default operation exists, the default cannot
  be archived. Otherwise an organization could retain one nominal but unusable
  default, because archived workspaces are inadmissible for new resource scope.

Workspace creation never infers owner, admin, member, policy, or cloud authority
from the caller, creator, account type, organization role, provider claims, or
Django staff state. An optional explicitly named initial membership may be
composed as a distinct authorized mutation; omission is valid and creates an
ownerless workspace. The account-aware creation transaction is the one creation
path for defaults and additional workspaces, parameterized by the structural
default designation and explicit grant input. The two current direct
`Workspace.objects.create` workflows must not remain independent policy paths.

An ownerless workspace is valid structural state. The existing "last owner"
rule applies once an owner exists; it cannot be used to fabricate an owner at
creation. Initial owner assignment needs an explicit organization-authorized
membership command. Ownership transfer remains a workspace owner command: the
target is an existing explicit workspace member, the target is promoted before
the actor is demoted, and the operation is atomic under the workspace lock.
After the principal cutover, membership and transfer target the stable principal
UUID rather than a Django user ID, email, provider subject, or creator relation.

Name uniqueness stays scoped to the organization and is enforced by the
database. The service owns normalization and the same length/nonblank validation
for HTTP, migration, admin, and composition callers. Database races translate
to one bounded classified conflict; raw `IntegrityError` text never crosses the
boundary.

## Authority boundary

Credential admission and customer policy remain separate:

- browser endpoints keep bearer-first `ApiTokenAuthentication`, then
  `SessionAuthentication`, with `IsAuthenticatedSession`; a valid platform token
  is still refused and unsafe session requests remain CSRF protected;
- the composition boundary resolves the authenticated Django user to ADR-066's
  `PrincipalRef` through the public `management.services` facade before calling
  principal-based workspace policy; `workspaces` must not import `management`;
- organization collection discovery, creation, and explicit first-owner setup
  use the existing persisted organization-admin seam and its audited superuser
  override;
- operations on an existing owned workspace use an explicit
  `WorkspaceOperation` and the single role matrix in `workspaces.roles`; and
- cached capabilities, SPA navigation, AccountMembership association, account
  kind, provider groups, Django groups/staff, token scopes, and cloud IAM are not
  workspace authority.

Organization authority does not silently become a workspace role. The explicit
initial-owner command is the bootstrap seam for a workspace created with no
grant. It must not be implemented by copying an organization admin into every
workspace, falling back to the creator, or adding an `owner_user` field.

Every lookup uses account and organization containment in the query or locked
service operation. A malformed UUID, missing row, wrong account ancestry,
legacy/ineligible organization, and unauthorized row produce the same opaque
tenant denial where their distinction would disclose existence.

## Archive boundary

Archive is the existing reversible `archived_at` marker. It is not deletion and
not a range lifecycle operation. It never deletes, rehomes, or mutates CMS or
Engine range bindings, provider resources, credentials, CTF state, membership,
quota evidence, or audit history. Restore only clears the marker.

Archive policy is operation-specific in `workspaces.services`. Do not add a
blanket archived check to every workspace authorization path: historical range
access, cleanup, audit, and restore need their separately accepted behavior.
List/search is active-only by default with an explicit archived-state filter,
and restore remains reachable without relying on a hidden row. A default
workspace archive is a classified conflict until default reassignment exists.

## Canonical incumbents to reuse

| Concern | Canonical incumbent | Required use |
| --- | --- | --- |
| Domain ownership and imports | ADR-001, ADR-046 as superseded by ADR-066, `.importlinter`, `scripts/check_layer_imports/layer_imports.yaml`, `check_model_fks` | Keep persistence/policy in `workspaces`; expose frozen scalar projections from `workspaces.services`; keep cross-domain references scalar. |
| Account hierarchy | `workspaces.models.Account`, `Organization`, `Workspace`; `workspaces.services._account`; `shared.identity_scope.ResourceScope` | Prove closed account kind and exact ancestry; use one locked, idempotent default/additional creation path; never infer installation or individual subdivisions. |
| Workspace authorization | `workspaces.services._authorization`, `_organization`, `_memberships`, `_lifecycle`; `workspaces.roles` | Reuse opaque denials, public UUID lookup, live grant checks, workspace mutex, central operation matrix, and conditional last-owner rule. |
| Principal identity | `management.services`; `management.principals`; `shared.identity_scope.PrincipalRef` | Resolve identity at the composition boundary; membership stores the validated scalar principal UUID and never imports management models. |
| HTTP validation | `shared.api.closed_serializer.ClosedSerializer`; typed DRF serializers; bounded query serializers | Reject unknown and repeated parameters, bound search/paging, validate primitive shape at HTTP, and repeat domain invariants in the service. |
| Errors | existing workspace classified outcomes; `shared.api.errors`; `shared.api.schema.ApiErrorSerializer` | Map once to the sanitized request-ID envelope; extend the existing mapper rather than create another exception hierarchy or return exception text. |
| Audit and logs | `shared.audit` vocabulary/attribution/strict writer; `shared.log_sanitize`; request IDs | Strict-audit mutations in their transaction using bounded internal IDs, action, field names, and outcomes. Log low-cardinality reason codes; do not log names, principal UUIDs, searches, rosters, or request bodies. |
| Persistence | Django migrations and named constraints; `transaction.atomic`; `select_for_update`; PostgreSQL test lane | Preserve IDs, use locks plus uniqueness to arbitrate races, and validate at-least-one/default and owner transitions where a check constraint cannot. |
| SPA transport/state | `frontend/src/api/client.ts`, `queryClient.ts`, `api/workspaces.ts`, generated `schema.d.ts`, TanStack Query | Reuse same-origin cookies, CSRF and request IDs; no component fetch; no mutation retry; invalidate lifecycle, organization, and membership context according to actual explicit grants. |
| SPA routing | `frontend/src/router.tsx`, `features/administer/routes.ts`, organization workspace layouts | Keep UUID route state and server-derived reachability. Do not grant staff merely to expose this UI or use membership context as the existence source for an ownerless workspace. |
| API publication | `config._drf_settings`, `shared.api.contract`, `openapi/v1.json`, `npm run gen:api` | Runtime serializers author the contract. Regenerate artifacts; do not hand-copy DTOs or make an ordinary breaking v1 change. |
| Runtime configuration | `config/env-manifest.json`, settings validators, installation schema/renderers, Kubernetes/Terraform inventories | This lifecycle needs no new env, secret, feature flag, cloud identity, provider setting, command, or worker payload. |

## Cross-cutting security and runtime layers

1. **Provider and session validation.** Existing OIDC/Identity Platform adapters
   continue to validate provider evidence and bind a Django session. PLAT-233
   consumes that authenticated actor only; it does not parse claims or infer
   customer authority from email, issuer, subject, or groups.
2. **DRF authentication and CSRF.** The bearer-first chain fails closed, the
   session-only permission rejects token principals, and Django
   `CsrfViewMiddleware` plus `SessionAuthentication` protects mutations. The SPA
   uses the canonical same-origin client. No `csrf_exempt`, alternate token, or
   direct `fetch` path is allowed.
3. **Transport shape.** Closed body/query serializers reject unknown or repeated
   keys, malformed UUIDs and booleans, overlong search/name input, and invalid
   paging before service invocation. Query bounds must match OpenAPI and the
   frontend instead of keeping the current free-form `_query_flag` behavior.
4. **Principal and ancestry validation.** `PrincipalRef` validates stable identity
   shape. `workspaces.services` then proves account kind, Account -> Organization
   -> Workspace ancestry, lifecycle admissibility, and the exact live operation.
   No null identifier or missing membership broadens scope.
5. **Persistence and concurrency.** The account/workspace mutex, atomic command,
   named unique/check constraints, and under-lock reauthorization cover default
   creation, name races, archive/restore, explicit membership bootstrap, and
   transfer. Model `clean()`, UI state, and preflight `exists()` checks alone are
   insufficient.
6. **Owning-domain policy.** Archive preserves scalar range bindings. CMS, Engine,
   CTF, remote access, quota, egress, model access, and range lifecycle keep
   their own existing gates. Workspace membership or lifecycle state grants none
   of their resource authority unless their accepted service seam explicitly
   composes it.
7. **Error envelope and enumeration resistance.** Domain outcomes map through the
   current workspace API mapper and `shared.api.errors`. External responses do
   not expose SQL, account kind/ancestry mismatch, target membership existence,
   provider data, or traceback text; the request ID remains the correlation
   handle.
8. **Audit and observability.** Trusted request attribution comes from
   `shared.audit` helpers. Successful security mutations are strict-audited in
   the transaction; denials/logs use bounded reason codes and metrics use only
   low-cardinality operation/outcome labels. Display names, emails, public UUID
   probes, principal UUIDs, tokens, headers, cookies, and bodies stay out.
9. **Secrets, configuration, OS and cloud exposure.** The feature introduces no
   secret or environment/config shape. Tenant/principal data never enters env,
   process argv, shell text, Kubernetes Job specs, guest metadata, provider
   labels, static bundles, Terraform values, or CI output. Browser responses keep
   the repository's SecurityMiddleware, CSP, Referrer-Policy, Permissions-Policy,
   secure-cookie, HTTPS, and HSTS controls.
10. **Published contract and workflow gates.** The runtime/OpenAPI/generated
    TypeScript chain remains one contract. Replacing the current v1 array list
    with a paginated envelope or replacing transfer `user_id` with a required
    `principal_uuid` is breaking and must use ADR-040's parallel-major or accepted
    retirement process. Additive evolution still runs OpenAPI drift and breaking
    checks, architecture/import/FK guards, focused backend/frontend tests, and
    real PostgreSQL race/constraint tests.

## Extensibility seams

The lifecycle collection query needs explicit, bounded `archived`, `search`, and
pagination parameters at the service/API boundary. The existing v1 array shape
cannot grow unbounded and cannot be silently changed; the next major contract
should use the repository's `PageNumberPagination` envelope. Keep filtering and
ordering in the authorized database query so a later account-level organization
filter does not require client-side filtering or materializing all rows.

The creation seam is the existing account-aware workspaces service operation,
parameterized by organization identity, `is_default`, and optional explicit
initial membership. A later accepted account type, default reassignment, or
workspace lifecycle action extends that one service and the central closed
vocabularies rather than adding branches across API, frontend, CMS, Engine, or
providers.

The identity seam is `PrincipalRef`. Human and service principals use the same
membership/transfer policy after S8; provider or credential type does not change
the workspace schema. Wire migration from user IDs must obey API versioning,
while persistence cutover remains a single canonical principal authority.

## Gotchas and anti-patterns

- Do not conflate Account, Organization, Workspace, Principal, Django User,
  provider identity, membership, owner role, CTF event/team, Django Group,
  Terraform workspace, cloud account/project, deployment, or range network.
- Do not call the personal-workspace resolver as a missing-scope fallback or mark
  a personal compatibility row as an individual account default.
- Do not keep both `_account` and `_lifecycle` workspace creation as independent
  policy workflows, or let a read/GET ensure defaults or create memberships.
- Do not grant the creator, organization admin, account member, first listed
  member, or individual principal an implicit workspace role.
- Do not require every workspace to have an owner at creation. Do not weaken the
  last-owner rule after an owner exists, fabricate a transfer target, or transfer
  by email/user name/provider subject.
- Do not archive the default until an accepted atomic default-reassignment
  operation exists. Do not make archive cascade, rebind ranges, or become a
  global authorization check.
- Do not trust the SPA's staff gate, navigation, cached capabilities, selected
  organization, or disabled buttons. Do not set `is_staff` to make organization
  administration reachable.
- Do not expose internal account/workspace IDs or keep `user_id` as the durable
  owner identity after principal cutover. Do not break v1 silently to remove it.
- Do not duplicate DTOs, validators, role matrices, exception trees, audit
  stores, logging formats, API clients, routers, pagination, or query parsing.
- Do not add env/config/secret/provider/cloud branches or place tenant data in
  logs, metrics labels, audit state, commands, worker payloads, or frontend
  bundles.

## Non-goals and implementation boundaries

- This preflight makes no runtime, migration, API, SPA, fixture, or production
  data change.
- No S8 partial cutover, dual-write/read fallback, feature-flagged authority,
  automatic legacy repair, or customer-specific fork.
- No organization/account lifecycle API, account-type conversion, default
  reassignment, workspace deletion, membership/invitation redesign, group
  lifecycle, or principal credential UI.
- No change to range ownership/access/cleanup, provisioning, CTF authority,
  quotas, egress, model access, cloud placement, IAM, secrets, network policy,
  workers, or Django admin escape hatches.
- No new authentication protocol, API token scope, OpenFGA evaluator/cache,
  custom policy engine, generic repository, background reconciler, or approval
  workflow.
