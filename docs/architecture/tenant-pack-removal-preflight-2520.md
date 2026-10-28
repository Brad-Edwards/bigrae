# Tenant pack removal architecture preflight (#2520)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.


Status: pre-implementation guidance, 2026-10-15. The issue is the delivery
contract; no formal requirement is attached. This note clarifies ADR-034-R13
without implementing removal or prescribing an implementation sequence.

## Maintainer decisions (2026-10-15)

The maintainer made these decisions after the preflight. Where they disagree
with the guidance below, these decisions apply.

| Topic | Decision |
| --- | --- |
| Retention | Hard-delete the `RaesPackageSource` row and its `ScenarioMetadata` and `ScenarioModelNeeds` overlays. History keeps scalar identities, audit records and runtime plugin pins. A later upload with the same name is a new pack. |
| Cleanup durability | `TenantPackArchive` records every uploaded archive. `TenantPackRemoval` captures exact keys before deletion. A leased reconciler (`reconcile_tenant_pack_removals`) retries pending cleanup on the portal identity, which owns delete authority on the bucket. |
| Storage versions | The service deletes live objects. The `raes-package-storage` bucket lifecycle rule deletes noncurrent generations after `noncurrent_version_retention_days` (default 7). |
| Fence | Every reference writer calls `cms.services.fence_pack_reference` in the transaction that commits the reference: cold reservation, warm prepare, warm claim, CTF event create and scenario change, and CTF spin-up task creation. Removal locks the pack row with `NOWAIT`. |
| Cleanup evidence | Unverified cloud cleanup of a terminal range is not a pack reference. Teardown uses the immutable compiled plan and the retained plugin pin, not the pack. A non-terminal range status is a reference. |

The soft-delete and tombstone guidance below is superseded. So is the
exclusion of bucket lifecycle changes from this work.

## Decisions and boundaries

- **Soft-delete registration; delete owned archive bytes.** (Superseded: see
  maintainer decisions.) Retain the
  `RaesPackageSource` identity, digest/version and audit evidence. Reuse
  `shared.db`'s `SoftDeleteMixin`, `SoftDeleteManager` and explicit
  `all_objects` manager, including the incumbent base-manager convention.
  Ordinary catalog, detail, binding, acquisition and launch queries must exclude
  tombstones, including staff/unfiltered catalog views. `ScenarioMetadata`
  remains the sole enabled/staff-only access overlay: disabling it is reversible
  access policy and does not implement uninstall. Conformance remains a trust
  fact; do not invent a `deleted` conformance status or hide lifecycle in
  `provenance`. Preserve the reference-only source contract.
- **Tenant-owned registrations only.** Authorize the addressed organization
  first and match its UUID to the stored owner. Organization-less registrations
  remain bootstrap/operator-managed, including object-backed ones. A tenant
  action cannot remove them, another organization's pack, or repository files.
  Staff status is not an ownership override; preserve the existing tenancy
  service's explicit superuser policy without allowing it to change the target
  pack's organization.
- **Refuse active references; no force option.** Reject removal while any
  non-terminal CTF event, future launch schedule, or live/pending range references
  the stable catalog id, across every revision digest. A confirmation dialog is
  an accidental-click safeguard, never permission to bypass the reference guard.
  Use the current digest as an optimistic precondition so a stale UI cannot
  remove a newly uploaded revision.
- **Remove current pack selections, preserve runtime history.** Engine owns
  `RuntimePluginPackBinding`; remove the pack's selections through
  `engine.services`. Retain `RuntimePluginRangeBinding`, its immutable
  `RuntimePluginPin`, installation records, operation inputs, delivery bindings,
  CTF hydration receipts/content and historical range/event rows. Uninstalling
  content does not retire an adapter, delete registry pull credentials, revoke
  runtime capability grants, destroy a range, or cancel an event.
- **Retain shared supply.** Feature demands are derived from packs by
  `cms.raes.feature_artifacts`, not a durable pack-owned demand ledger.
  Tombstones stop future registration/bootstrap acquisition and launch demand.
  Engine's `AcquiredFeatureArtifact` inventory and content-addressed delivery
  bytes are shared supply; retain them and already claimed acquisition attempts.
  Do not infer exclusive ownership from the absence of another current catalog
  declaration. Image inventory and prepared artifacts are likewise outside
  uninstall's ownership.

## Ownership, references and concurrency

CMS owns removal. CTF owns event and schedule interpretation; Engine owns runtime
state, cleanup evidence and bindings. `.importlinter` and
`scripts/check_layer_imports/layer_imports.yaml` prohibit CMS importing CTF and
cross-layer model access. `ctf.bridges` already calls `cms.services` in the
permitted direction; it is not permission for a reverse import.

There is no existing pack-reference guard. Supply the small missing CTF read
seam through the existing composition-root pattern: a narrow typed callable
contract, wired in `config.apps` from a public CTF service to the CMS consumer.
Follow `shared.model_access.projection_port`'s bind-once, fail-closed pattern;
put neutral cross-domain values in `shared`, not CTF models/querysets in CMS.
Do not build a generic dependency registry, new workflow framework or duplicated
event repository. Missing binding, query failure or an unresolvable reference
means removal is unavailable, never that no references exist.

The authoritative guard must cover:

- `CTFEvent.scenario_id` using `ctf.enums.EVENT_TERMINAL_STATUSES`, including
  draft, registration, active and paused events. Dates alone do not establish
  terminal state. `CTFScheduledTask` pending/claimed launch-producing tasks must
  be considered through the scheduler's current task/status and claim-fencing
  semantics, even if the parent event looks terminal. Cleanup-only tasks that
  consume retained runtime inputs must remain executable.
- CMS `RangeInstance` and its `Request`, plus Engine runtime/launch state through
  public services. Pending, provisioning, paused and destroying are references,
  as are warm-pool generations, CTF spares and queued dispatches. Use canonical
  `shared.enums` vocabulary. Neither soft deletion nor `FAILED` alone proves
  cloud absence: reuse `engine.services.project_range_cleanup_outcome` and
  `is_cleanup_verified_absent` evidence (ADR-063) when cleanup is unresolved.
  Inspect historical managers when necessary; an active-only manager can hide
  an unfinished cleanup obligation. Unknown/inconsistent states fail closed.
- The pack id is a scalar in these records, not a protecting foreign key.
  Cross-organization references caused by old or inconsistent data must still
  prevent deletion, without disclosing their names or identities to this actor.
  Return an opaque blocker for a reference the actor cannot inspect. CTF event
  visibility/capability checks remain CTF-owned; organization admin status does
  not grant blanket access to every event's private details.

A count-then-delete inside `transaction.atomic()` is insufficient under normal
database isolation: locking the source row does not lock future event inserts.
Use that stable source row as the admission fence, and require every writer that
creates/changes a pack reference to participate through the CMS service seam.
This includes event create/update, scheduling, cold launch, warm claim/preparation,
pack update and binding. Recheck active state and revision while the lock is
held through committing the reference/reservation. An already reserved launch
blocks removal until its existing lifecycle converges; do not hold a database
lock over cloud I/O or compilation. A prior catalog lookup is not a reservation.

Respect existing organization/workspace mutexes and the Engine rule of locking
installation before binding. Specify and exercise one compatible lock order
across these writers; the removal reference projection must not acquire event
locks in reverse order to event admission. Membership revocation must not race
the authorized mutation: reuse `get_organization_profile(..., lock=True)` inside
the mutation transaction. Multi-row installation locking needs deterministic
ordering. SQLite tests cannot establish these concurrency guarantees.

## Storage completion and recovery

`_tenant_pack_upload` generates unique immutable objects at
`<RAES_PACKAGE_PREFIX>/tenant-packs/<organization UUID>/<random UUID>.tar.gz`.
Updates in `_content_ingestion._replace_existing` retain old objects but overwrite
the current source reference. No pack-owned archive inventory currently covers
those earlier objects. Deleting only the current `package_ref` does not satisfy
the issue; listing an organization's whole prefix risks deleting unrelated packs.

Retain the minimum CMS-owned per-pack revision/object references needed to prove
ownership and resume cleanup. Capture upload/update references transactionally
with registration. Existing installations need reconciliation from trusted
registration revision evidence; do not guess ownership from a filename or
digest, use an audit table as the ongoing work queue, or silently claim complete
cleanup when evidence is missing. Audit evidence access stays behind its owning
boundary; migration/reconciliation does not authorize a new tenant audit-read
permission. Legacy objects that cannot be attributed safely need explicit
operator reconciliation before removal can report completion.

Use `shared.cloud.get_object_storage` / `ObjectStorage` and `CloudStorageError`;
CMS must not call cloud SDKs, shell out to storage CLIs, or implement a second
storage client. Select bucket/prefix from operator configuration and retained
server-owned location facts, never request fields. Validate every cleanup target
as an exact normalized owned key, rejecting traversal, URLs, namespace escapes
and references shared with another registration. The source schema's bounded
single-line reference check is **not** a destructive-operation containment check.
Optional lock/report refs are not implicitly owned archive objects.

Database and object storage are not one transaction. Commit the admission fence,
strict removal-request audit and durable cleanup targets before destructive I/O;
after that fence, the pack is hidden and cannot acquire new references. Retain
pending progress across partial deletion, process death, timeout and audit failure.
Retry exact captured keys idempotently; do not reopen admission after bytes have
been removed. Completed removal means all owned archives are absent and the
completion audit is durable. A pending result must say cleanup is pending; never
return completed success just because a callback was queued. Repeated authorized
requests must reach the tombstone to resume or return the same completed result.

Reuse the durable claim/fence/retry conventions in
`engine.services._preparation_controller` and the existing outbox drainers, but
keep cleanup state CMS-owned. `RangeEventOutbox` is notification-shaped and
`ProvisionerLaunchIntent` is for provisioner dispatch; neither is a generic pack
deletion queue. `transaction.on_commit` may wake a durable consumer but cannot be
the only record of work. The S3-first `cms.assets.services.delete_agent` pattern
does not provide the required crash, concurrency or audit guarantees here.

Provider behavior needs explicit treatment: GCS `delete_object` currently wraps
missing objects as `CloudStorageError`, whereas S3 deletion is normally
idempotent. Normalize verified absence at the provider seam; permission failures
and transient errors are not absence. The configured GCP assets bucket enables
versioning (`platform/terraform/gcp/modules/portal/gcs/main.tf`); deleting a live
object alone can retain noncurrent generations. Cleanup must remove all retained
versions of each owned archive key to claim its bytes are gone, through a narrow
provider-neutral capability if the existing protocol is insufficient. Preserve
bucket retention/hold/soft-delete controls and report retained bytes or blocked
cleanup truthfully instead of weakening them. Shared bucket deletion, broad
prefix purges, and bucket-wide lifecycle changes are outside this service.

## Cross-cutting gates and canonical incumbents

Paths below are relative to `shifter/shifter_platform/` unless stated otherwise.

| Layer | Reuse and required behavior |
| --- | --- |
| HTTP authentication and authority | `cms.api.tenant_packs`, `runtime_plugin_packs`, `shared.api.permissions.IsAuthenticatedSession`, `workspaces.services.get_organization_profile`. Same-origin session authentication and CSRF on unsafe methods; no token-scope expansion. Resolve ownership in the service, not from UI capability flags or request-supplied tenant claims. |
| Request and persistence shape | `PreparationSerializer` rejects unknown fields; use bounded pack identifiers and canonical digest syntax. Keep DRF transport validation separate from service invariants and `shared.schemas.raes_package_source.validate_package_source` / `RaesPackageSource.save()`. No duplicated source DTO or RAES parser, and no credentials/runtime state in provenance. Soft-delete managers must not bypass model validation or uniqueness constraints. |
| Errors and disclosure | Reuse `shared.exceptions.ValidationError`/CMS errors and `OrganizationAuthorizationError`; no parallel exception hierarchy. Use `shared.api.errors.api_error_response` with a stable conflict code/status and allowlisted bounded blocker details (kind, visible id/name, status, count/continuation). Unauthorized/missing targets remain opaque. `_normalize_detail` rewrites DRF `ErrorDetail`; arbitrary `str(exc)` is not a blocker contract. Bound pagination and use existence checks so truncation never permits deletion. |
| Audit and observability | Reuse `AuditEvent`, `RequestAudit`, `audit_log(..., strict=True)`, request attribution, and `safe_log_value`. Record actor/principal, organization, catalog id, digest, request id and requested/completed outcome. Fail before storage deletion if the request audit cannot commit; retry pending completion if its audit fails. Record bounded failure codes/counts and correlation for cleanup operators; no bodies, registry credentials, signed URLs or raw provider exceptions in responses/audit. |
| Storage identity and credentials | Existing provider adapters, configured workload identity and bucket-owner safeguards. No new secret input or external service. Retain exact storage location for retries so a later configuration change cannot redirect cleanup. Scope delete/list-version capability to the intended archive bucket and service identity; existing worker read-only access is not deletion authority. Inspect GCP portal IAM and AWS portal EKS IAM rather than copying provisioner credentials. |
| Config, host and runtime | `config/_raes_settings.py`, `_env_manifest.py`, `env-manifest.json`, and `generate_env_manifest` own settings discovery. Prefer existing settings. Any necessary retry knobs use that manifest and canonical Helm/runtime wiring; startup validates them. SDK calls require no subprocess, argv token, filesystem extraction, guest command, plugin execution or broad environment binding. A durable consumer must actually run under an explicitly configured identity with storage permission; do not assume scheduler/worker credentials match the portal. |
| API and UI contracts | `config/api_urls.py`, `config/_drf_settings.py`, `cms/api/urls.py`, drf-spectacular and `api_contract` own the versioned operation. ADR-040 permits a new independent removal operation without changing existing binding/update semantics. Generate `openapi/v1.json` and `frontend/src/api/schema.d.ts`; do not hand-author parallel TS schemas. Reuse `apiFetch` (CSRF), React Query organization keys in `api/adapter-packs.ts`, `ApiError`, `ConfirmDialog`, and `AdapterPackBindings`. Render authorized structured blockers explicitly: `describeMutationError` alone shows only the top-level message. Invalidate affected catalog and binding queries after accepted/completed removal; show pending truthfully and preserve errors for retry. |
| Architecture and verification | Repository-root `AGENTS.md`, `.ground-control.yaml`, `.gc/plan-rules.md`, `.importlinter`, `scripts/check_layer_imports/layer_imports.yaml` and ADRs 001, 034, 040, 045, 046, 063 and 066 remain authoritative. Run ADR guard; implementation also needs relevant import/Ruff, migration, API contract and frontend checks. Helm/IAM/workflow changes trigger their existing stack-native checks rather than exemptions. |

## History, extensibility and acceptance evidence

Ended/cancelled/archived events and destroyed ranges render from retained
identifiers, hydrated content and runtime records, with a safe historical label
when a catalog lookup finds no active entry. Historical rendering must not
download a deleted archive or make a tombstone selectable. Keep existing history
authorization. `RangeInstance.scenario_id`, event serializers and CTF hydration
receipts already carry scalar identity; do not introduce cross-layer foreign
keys, synthesize a replacement pack, or rewrite historical records to a successor.

The extensibility seam is the **authorized organization + stable catalog id +
expected revision**, with exact owned storage targets behind the storage adapter
and CTF-owned typed reference projections behind composition. Never key policy
on display name, scenario identity or provider choice. Same-name uploads derive
the same catalog id (`uuid5`) and uniqueness survives soft deletion: check
`all_objects` and return an explicit conflict, not a raw integrity failure or an
accidental resurrection. Explicit reinstall/restore can later reuse the stable
identity only after pending cleanup is fenced off; it must not let old cleanup
delete new bytes. Restore is not part of #2520.

Acceptance evidence must exercise service and real API/UI boundaries: every
non-terminal event state and future/claimed provisioning schedule, terminal
history, all range sources including warm/spare and queued launches, unresolved
failed cleanup, absent guard wiring, cross-org/staff/token/CSRF denial, stale
digests, unknown fields, and binding-list disappearance with retained range pins.
Cover multiple archive revisions, malicious/foreign keys, provider missing-object
semantics and versions, partial failure/restart/retry, audit failure before and
after deletion, and upload/bind/event/range admission racing removal. Use real
first-party services with synthetic fixtures and mock external storage/provider
boundaries (ADR-019); use PostgreSQL transaction tests for locks. Verify ended
event and destroyed-range pages with storage inaccessible, not merely row counts.
Extend the incumbent `tests/cms/test_tenant_pack_upload.py`,
`test_runtime_plugin_packs.py`, Engine runtime-binding and shared cloud-storage
test patterns, plus `frontend/src/features/administer/AdapterPackBindings.test.tsx`;
do not replace them with a mocked first-party call graph.

Non-goals: implementing removal in this preflight; a purge/restore platform,
general dependency graph, pack-format change, new entitlement system, adapter
uninstallation, shared artifact garbage collection, platform-pack removal,
automatic cancellation/destruction of references, or weaker security/CI gates.
