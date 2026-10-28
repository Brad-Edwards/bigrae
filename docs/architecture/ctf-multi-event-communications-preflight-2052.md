# Multi-event communications and scheduling boundaries (#2052)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.

Inspected 2026-10-27 at `4b44a4285`. Scope: CTF-010 in #2052, with CTF-008
and CTF-012 at the same API/console boundary. This is design guidance, not an
implementation plan or evidence that acceptance is complete. Application paths
below are relative to `shifter/shifter_platform/`.

## Decision and observed gaps

Build on the existing communication ledger and scheduler. CTF-1001 already has
an executor: `ctf/management/commands/run_ctf_scheduler.py` and
`ctf/models/scheduled_task.py:CTFScheduledTask`. Older trace links naming
`ctf/models.py`, `ctf/services/event.py`, or the notification model do not define
new implementation locations. ADR-051 owns communications; ADR-065 retires
legacy notification writes. ADR-066 stages a later coordinated authority cutover.

The current API already authors multi-event campaigns within one workspace,
appends content revisions, releases/schedules, and cancels through services.
The remaining gaps and hazards are specific:

- `frontend/src/api/ctfCommunications.ts` and `MonitoringPage.tsx` compose for
  one event. There is no multi-event preview operation. The campaign summary
  exposes lifecycle/channel names and target count, but no revision content,
  preview audience, due declaration, or per-event/channel delivery totals.
- `ctf/services/event/scheduling.py:_reschedule_event_tasks` cancels every pending
  task anchored to the event, then recreates lifecycle/challenge tasks only.
  This can strand a scheduled communication intent. Preserve independent
  absolute-time declarations when event times change; superseding event-derived
  work must reconcile its intent and execution task through their existing owners.
  Terminal task failure must also remain distinguishable from a healthy schedule.
- Release defaults to the latest revision if none is supplied. A previewed send
  must supply the existing `revision_id`; otherwise an intervening edit can change
  what is sent. Immutability comments are not enforcement: audit `bulk_create`,
  queryset updates, target deletion and revision/campaign association changes,
  which bypass some model validation.

## Scheduler owners and invariants

The [current communication API guide](../technical/shifter_platform/ctf-communications.md)
describes the incumbent API. The constraints below apply to its extension;
they do not establish another workflow owner.

| Concern | Canonical incumbent and boundary |
| --- | --- |
| API and authoring | `ctf/api/organizer/communication.py`, communication serializers/schema, `CampaignDraft` and `communication/campaigns.py` own HTTP translation, projections and authoring. Extend the existing versioned API and Django persistence; no repository or DTO family is missing. |
| Discovery and audience | `communication/selection.py:visible_campaigns` filters complete-target authority before paging/counting. `communication/audience.py:resolve_recipients` and `viewing_participant_q` own event-qualified eligibility; never deduplicate across events by email or user. |
| Task execution | `CTFScheduledTask`, scheduler command and `ctf.enums`. Preserve one-shot dispatch, claim tokens, task heartbeat, bounded stale recovery, retries and shutdown. No new task schema, queue or registry. |
| Event automation | `ctf/services/event/scheduling.py` and the existing event state machine own lifecycle/reminder/challenge scheduling. Intrinsic lifecycle effects are distinct from human-authored communication release. |
| Communication timing | `ctf/services/communication/scheduling.py` and `release.py` own atomic declaration/task creation, due-time admission, idempotency and expiry. The task event is a routing anchor; the intent retains the full campaign scope. |
| Cancellation and transport | `communication/lifecycle.py`, `delivery.py`, `adapters/contract.py` and `inbox.py` retain their fences, leases, retry and receipt semantics. Here `communication/` abbreviates `ctf/services/communication/`. |
| Range lifecycle | `ctf.bridges`, CMS lease services and `cms/management/commands/reconcile_range_events.py` own provisioning/expiry. A communication schedule or cleanup-task toggle cannot extend a persisted range lease. |

Keep these facts separate:

- A preview pins the existing revision and target/audience/channel/trigger policy;
  it creates no release, task, snapshot, delivery command or receipt. Scheduled
  recipient snapshots are resolved at due release. Preview counts cannot promise
  a reserved future audience. Submit the explicit previewed `revision_id`.
  Bound preview query/materialization cost through the resolver and existing
  abuse budgets: checking the length after its current full-list materialization
  is insufficient. Do not copy audience policy into a controller.
- One current campaign has one workspace and one absolute due instant. Authorize
  every target independently, with atomic admission and opaque denial on an
  unavailable target. Separate workspace campaigns cannot imply one atomic send.
  Deleted targets must not silently shrink the persisted authorization scope.
- Preserve authored `due_at` while retry/resume changes task `scheduled_for`.
  Use `validate_trigger_spec`'s aware UTC normalization and show the interpreted
  timezone/instant, including daylight-saving ambiguity. The existing grace window
  and EXPIRED disposition bound lateness. Session-only `request_early_release`
  remains the authorized run-now path; changing a task timestamp grants nothing.
- HTTP 202, campaign/intent lifecycle and scheduler completion are admission or
  execution facts. Report delivery from existing event/channel-qualified attempts,
  inbox availability from committed receipts, and read/ack from deliberate receipt
  actions. Preserve mixed outcomes, zero recipients, purged history and legacy
  aggregate-only evidence. No persisted aggregate state machine or generic `sent`.
  Aggregate in bounded queries without multiplying receipt counts by channel joins.
- Cancellation guarantees stopping unclaimed delivery commands. A committed claim
  ends the recall guarantee; pre-I/O suppression is best effort. Already committed
  inbox availability and external acceptance cannot be recalled. Campaign/event
  cancellation, participant removal and range replacement have distinct scopes.
  Email is unavailable pending #1525; never silently substitute in-app or queue
  unsupported email indefinitely. WebSockets remain reference-only wake-ups.

## Cross-cutting gates the design must pass

| Layer | Required reuse and satisfaction |
| --- | --- |
| Authentication and application authority | `config/_drf_settings.py`, `active_actor_user`/`request_actor`, shared `require_scope`, and exact communication read/write scopes retain bearer-first fail-closed and session-CSRF behavior. Neither scope implies the other; a POST preview needs an explicit read-scope declaration rather than inheriting unsafe-method write scope. `admission.reauthorize` checks the original live user/token pair, workspace membership/archive mutex and notification authority on every target at due time and replay. The superuser event override does not bypass workspace admission. System/origin/early-release privilege is never caller-supplied or inferred from absent identity. |
| Authority evolution | `ctf/api/operation_policy.py` classifies intrinsic lifecycle tasks, reauthorized communication release and retired notification work. Preserve coverage for every task/operation. At ADR-066 S8 use the shared credential/principal/ResourceScope and owning policy services, retaining the original credential ceiling and resolved ancestry. Do not activate mixed legacy/OpenFGA authority, fabricate a human for a service, or treat missing workspace as installation authority. |
| Parsing, schemas and content | `CommunicationJSONParser`/`ClosedJSONParser`, `ClosedSerializer`, `validate_query`, `communication_contracts` and communication enums own closed shapes, duplicate rejection, bounds, UUIDs, UTC and content policy for HTTP/internal callers. The 1 MiB envelope, UTF-8 body-byte, reference and target limits are distinct. Preview reuses the pinned profile on `MarkdownContent` under CSP; no raw HTML, remote media, caller-defined link hosts or second renderer. |
| Persistence and pressure | Reuse event-first primary-key lock order, live actor/token/workspace locks, campaign/revision serialization, database uniqueness and nested-savepoint replay handling. Declaration plus task and release plus snapshots/commands/inbox/strict audit commit at their existing atomic boundaries. `communication/backpressure.py` owns shared rate/backlog/audience budgets and fail-closed outages. Claim tokens fence stale settlement; transport I/O stays outside locks. `full_clean` does not protect bulk writes. |
| Secrets and privacy | Reuse `EncryptedStringField`, retention/removal/purge hooks and the participant credential owner. Communication-task metadata carries bounded non-secret intent references; lifecycle tasks retain their existing closed metadata fields. No credential, signed URL, coordinate, body or provider payload in argv, probes, logs, errors, audit, metrics, ConfigMaps or browser persistence. No new external processor or preview recipient store. Ordinary reports expose bounded aggregates, not recipient rosters. |
| Errors, audit and observability | Keep `CTFCommunicationError`/`CTFError` → `ctf/api/_base.py:ctf_error_response` → `shared.api.errors`, with authored bounded messages/request IDs and deliberate 400/404/409/429/503 mappings. Never expose raw task errors or `str(exc)`. Reuse `communication/audit.py`, release audit, `shared.audit`, structured logging, `shared.log_sanitize` and closed low-cardinality `communication/metrics.py`. Required-audit failure rolls back admission. Global audit cannot become tenant history through guessed campaign filtering; scoped evidence/read authority must exist first. |
| Environment and configuration | No console projection needs a new setting. If policy needs a parameter, extend `_ctf_communication_settings.py`'s typed bounds/relationships, `product_env`'s BIGRAE/SHIFTER alias conflict gate and `_env_manifest.py`/generated manifest with parity tests. Manifest shape is `name/default/source_file`; Helm `runtimeEnv` accepts uppercase names and scalar values, rendered as non-secret strings. Retain `shifter/installation` schema/loader/render/runtime-inventory gates, `scripts/gcp/render_runtime_env.py` and Helm values-schema/render checks. All processes receive the same validated policy; secret references stay on the existing hydration path. |
| Host and runtime | Keep supervised scheduler/delivery commands and their separate process/task heartbeats; no web-import threads, cron, host locks or secret-bearing shell wrappers. Helm is the shared GKE/EKS package. Preserve its values schema, SQL duties, security contexts, NetworkPolicy, disabled API-token automount and dedicated provisioner-launcher admission. Scheduler receives no privileged Job/Secret mutation. Consult current deployment guidance rather than historical EC2/ECS preflights. Static GCP fixtures and compose launch/probe contracts remain relevant when touched. |
| Browser and accessibility | Extend `frontend/src/api/ctfCommunications.ts` using same-origin `apiFetch`, TanStack Query, generated types, shared Dialog/form components and `describeMutationError`. No bearer storage or second fetch client. Key cached/private state by workspace, target set and revision, invalidate previews on edits and clear private state on account/context changes. Preserve keyboard/focus, labelled times, bounded paging and explicit retry/error recovery; sockets remain hints to read the durable inbox. |

## Extensibility, evidence and exclusions

The seams are already present: explicit campaign targets and closed audience/trigger
variants, pinned revision/content profile, stable campaign/occurrence identity,
`DeliveryCommand`/`DeliveryOutcome`, and server-owned operational budgets. Parameterize
the common client operation by validated target set within one workspace; keep
the single-event path as that same operation with one target. Future channels
extend the existing adapter contract, while per-event/channel reports group its
existing enum/status facts. Neither requires routes per transport, arbitrary
callbacks, a plugin registry or a generic workflow engine.

Use existing `tests/ctf/test_communication_*`, scheduler claim/concurrency tests,
workspace operation and authorization-operation-map tests. Evidence must cover
admin/organizer/moderator/judge/participant and cross-tenant targets, removed
staff/token revocation at due
time, preview without effects, stale revision submission, event-time changes with
independent schedules, terminal task/intent divergence, release/cancel/claim races,
safe partial-delivery aggregation, retention and audit failure. Real PostgreSQL
tests prove locking; Redis/ASGI tests prove body-free wake-up/replay. Browser tests
must cover keyboard/focus, labelled timezones, loading/error/retry recovery and
retained draft/occurrence identity. The current hook retains a campaign after a
release failure but cannot recover a lost create response; do not claim end-to-end
exactly-once creation or silently resend ambiguous requests.

Run the ADR guard for this architecture change. Subsequent implementation must
also satisfy affected Ruff/import checks (`.importlinter` and the per-symbol
`scripts/check_layer_imports/layer_imports.yaml`), generated OpenAPI drift and
compatibility gates (`manage.py api_contract`, `npm --prefix frontend run gen:api`),
frontend type/test/accessibility checks and applicable root Makefile PostgreSQL/
Redis lanes. Runtime changes additionally require chart schema/render tests and
the Kubernetes, Terraform or workflow validators in `AGENTS.md`. A passing registry
check is structural evidence, not proof of delivery reliability.

Non-goals: implementing #2052 in this preflight; replacing the scheduler or adding
a second queue, outbox, DTO vocabulary, exception hierarchy, authorization engine
or persistence layer; recurring scheduling, arbitrary RAES/range ingress, email
transport #1525, cross-workspace atomic delivery, secret issuance, recipient export,
external exactly-once delivery/recall, or ADR-066 S8 activation. Range provisioning
and expiry stay behind `ctf.bridges` and CMS lease/reconciliation services; a
communication schedule or cleanup-task toggle cannot extend a range lease. Do not
revive retired `CTFNotification` writers or change historical migration evidence.
