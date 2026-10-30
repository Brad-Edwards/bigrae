# CTF-701 event lifecycle architecture preflight

Assessment: 2026-10-30, against this checkout, including existing uncommitted
#528 work. This is design guidance, not an implementation plan, acceptance
record, or authorization to change deployment services. It applies existing
service, identity, communication and audit boundaries; no new ADR or exception
is needed. The supplied CTF-701 statement includes automatic end from paused.

## State authority and scope

Keep `ctf.enums.EventStatus`, `VALID_TRANSITIONS`/`validate_transition`,
`ctf.models.CTFEvent` and `ctf.services.event._lifecycle` as the state owners.
There are seven states and eleven directed edges, including the conditional
edge below. Historical trace text saying “all 10” is not acceptance evidence.

| Source | Permitted targets |
| --- | --- |
| draft | registration, cancelled |
| registration | active, cancelled |
| active | paused, ended, cancelled |
| paused | active, cancelled; ended **only through scheduled end** |
| ended | archived |
| cancelled, archived | none |

Initial state remains draft. “Scheduled” and “completed” are historical event
aliases, not additional states. Scheduled-task completion, range readiness,
communication acceptance and read receipts are separate facts. The existing
`EVENT_TERMINAL_STATUSES` includes ended for edit/admission policy; that does
not prohibit its explicit archival edge. Archive is not soft deletion, data
purge, cancellation, or evidence that resources were destroyed.

The conditional edge needs one authoritative service guard that checks trusted
scheduled-end execution, the live event and `now >= event_end` under its lock.
Execution after downtime is still the scheduled occurrence; equality must work.
Manual end remains active-only even after the deadline. Neither an unqualified
edge in `VALID_TRANSITIONS` nor a raw status assignment outside validation is a
complete state machine. Keep the ordinary transition predicate's meaning clear
and put the contextual admission in the same lifecycle owner; do not copy a
second transition table into scheduler, model, API or frontend. A Python
`automatic=True` argument is source selection, not proof of authority: no HTTP
body, task metadata or missing interactive actor may grant system authority.

#528 adds organizer delivery for automatic start/end and late spin-up. Reuse the
[organizer design](../architecture/ctf-organizer-alerts-design-528.md) and
[alert preflight](../architecture/ctf-organizer-alerts-preflight-528.md) for that
recipient/transport work. The alert preflight's earlier statement that paused
cannot end is superseded by the supplied CTF-701 statement and this boundary.
Do not turn the alert issue into a general lifecycle rewrite.

## Boundaries the design must pass

Paths in this table are relative to `shifter/shifter_platform/` unless stated.

| Layer / canonical incumbent | Required reuse and satisfaction |
| --- | --- |
| HTTP authentication and scopes | `ctf.api.organizer.lifecycle.EventLifecycleView`, `ctf.api._base.CTF_ORGANIZER_PERMISSIONS`, `shared.api.principals.authenticated_credential`, session/CSRF and token authentication. Retain the declared event-write token scope **and** exact live `event.manage_lifecycle` permission; a token scope alone grants nothing. Temporary participant credentials cannot manage events. |
| Principal, policy and tenancy gates | `ctf.services.authorization.admit_event_actions`, `ctf.services.actors.EventActionAdmission.validate`/local credential fencing, `unified_authorization`, `credential_scope`, shared `CredentialContext`/ceiling and authorization contracts. Prepare policy admission before SQL locks, then validate it against the locked event binding. `services.event._update.update_event` demonstrates this split. Reuse `workspaces.services.resource_scope_from_ids`/`resolve_resource_scope` for live ancestry, including account-only events. No owner/group/superuser shortcut, fabricated human for a service, copied scope JSON, or local hierarchy validator (ADR-066). Policy outage remains unavailable/denied. |
| Request parsing and DTOs | Keep `EventLifecycleRequestSerializer` and `EventWriteSerializer`; status and automation authority remain absent from generic event writes. The lifecycle serializer currently uses plain DRF `Serializer`, unlike closed event writes. If its shape changes, use existing `shared.api.closed_serializer.ClosedSerializer` and `shared.api.strict_json.ClosedJSONParser` for unknown/duplicate-field rejection, rather than a new parser. Date fields use DRF and the existing event service/model validators; no new date schema. |
| Persistence and validation | `CTFBaseModel.save()` calls `full_clean()`; `CTFEvent.clean()` owns time ordering and immutable placement, not historical transition validation. Reload through the soft-delete-aware manager and serialize mutations on the event row. Retain model saves and `ctf.signals` invalidation; raw `QuerySet.update(status=...)` bypasses both validation and signals. Ordinary field choices/check constraints cannot prove previous state, execution source or a scheduled occurrence. No repository wrapper or new transition-history table is needed. |
| Durable workflow and claims | `event.scheduling`, `CTFScheduledTask`, `ScheduledTaskType`/`Status`, and `run_ctf_scheduler` own task derivation, claims, retry, heartbeat and recovery. Preserve `ctf.api.operation_policy`'s command/scheduled/external-effect inventories and ADR-065's transaction-local ledger-writer claim fence. Intrinsic lifecycle automation uses its committed schedule; provisioning retains its recorded initiating credential through `shared.operation_authority` and current admission. No creator impersonation or second scheduler/queue. |
| Error boundary | Reuse `CTFStateError`, `CTFValidationError`, `CTFPermissionError`, `CTFNotFoundError` and the communication error family. Map through the existing CTF helpers/`ctf_error_response` into `shared.api.errors`: malformed request 400, forbidden 403, missing 404, refused transition 409, dependency loss unavailable. Do not expose `CTFError.details`, Django validation values, task errors or raw provider exceptions as a new public envelope. A false transition result is not automatically successful task work. |
| Audit and observability | `ctf.services.audit`, organizer `_audit`, `shared.audit.AuditEvent`/`StateChange`, strict audit and request/principal attribution own evidence. Database mutation and required audit commit together; irreversible work uses intent-before/effect/outcome boundaries outside domain locks. Scheduled actions use system attribution. Reuse module loggers, `config.logging.ECSFormatter`'s supported fields and `shared.log_sanitize`; bounded state/source/reason, event/task/occurrence correlation, due/observed time and lag suffice. IDs belong in logs, not metric labels. Truncation does not redact; `logger.exception` may disclose nested provider text. |
| Secrets, delivery and OS exposure | Lifecycle itself needs no secret or new service. #528 uses `ctf.communication_contracts`, existing campaign/intent/snapshot/`DeliveryAttempt` constraints and worker, `ChannelAdapter`/`DeliveryCommand`/`DeliveryOutcome`, `shared.email.attempt_email` and deadline enforcement (ADR-071). Coordinates stay in `EncryptedStringField` and decrypt at the email adapter. Reuse entrypoint secret hydration; no tokens, passwords, addresses, message bodies or provider payloads in argv, shell tracing, task metadata, probes, audit, metrics or logs. Never open local secret files for this feature. |
| Config and runtime validators | No new knob is needed for the state rule: `event_end` is authoritative. Any necessary operational timing parameter uses `config.settings._env_int`, bounded relationships and the established `config/_env_manifest.py` `EnvBinding` shape; GCP rendering uses repository-root `scripts/gcp/render_runtime_env.py`, and AWS uses existing bootstrap/env wiring. Keep `TIME_ZONE=UTC`, `USE_TZ`, aware instants and presentation-only event timezones. Preserve compose, AWS portal bootstrap/redeploy and Helm scheduler command/probe/RBAC contracts; one replica is not a correctness guarantee. |
| API/client and repository gates | If public actions change, use DRF spectacular, `openapi/v1.json`, the existing compatibility/drift gates and generated `frontend/src/api/schema.d.ts`; `frontend/src/api/ctfAdminOps.ts` remains a client, never the state authority. Repository-root `.importlinter`, `scripts/adr_guard`, `.pre-commit-config.yaml`, `.github/quality-path-filters.yaml` and `_quality.yml` remain enforcement owners. `.ground-control.yaml` supplies canonical repository identity; requirements/traceability are reviewed files, not a second database. |

## Material risks in this checkout

- **Mutation races and duplicate entry points.** `activate_event`, resume and the
  new automatic end lock their event, but open-registration, pause and archive
  still pass caller instances to unlocked `_transition_event`. Concurrent pause,
  end, cancel, schedule edit or deletion must never overwrite a committed newer
  state. `start_event`/`end_event` also remain exported with different return and
  end-effect behavior. Preserve facade/patch conventions in `event.__init__`,
  but make every supported path obey the same guards and effects; do not create
  another independently maintained manual/automatic lifecycle implementation.
  Every path to ACTIVE must retain
  `content_hydration.assert_event_content_hydration_ready` under the event lock,
  including resume; bypassing it can score against stale content or flags.
- **Claims do not yet fence effects.** The executor conditionally settles with
  its token, while automatic lifecycle services currently receive no claim or
  authored-occurrence proof. Check current task ownership and schedule under the
  event-first locking discipline before state/effect declaration. Retry-mutated
  `scheduled_for`, a replacement task ID, or today's ACTIVE/ENDED state is not
  historical transition evidence. `run_task_now` must not waive scheduled-end
  eligibility. A stale claimed task cannot end a rescheduled or cancelled event.
- **Crash-safe effects.** State, required audit, schedule/cancellation work and
  durable alert intent must have a defined commit boundary. A callback alone
  loses work after commit. Existing `declare_organizer_alert` catches a
  communication-domain refusal: deterministic IDs do not recover a declaration
  that was never persisted. Keep failed handoff recoverable through existing
  task/intent owners without rolling back end/cancel for a transport outage.
  `finish_ended_event` replay uses the current ENDED state; intervening archive
  must not lose pending effects, and replay after a *manual* end must not invent
  an automatic-end alert. Result recomputation/notice, lease cleanup and alert
  delivery need separate idempotency assertions, not an “exactly once” claim.
- **Outer transactions and lock order.** The HTTP lifecycle view wraps `_apply`
  and audit in `atomic()`, while complete/cancel can invoke cleanup or policy
  work before returning. Exiting an inner transaction does not release that
  outer transaction. Follow existing prepared-admission and external-effect
  patterns instead of holding event locks during cloud/mail/provider I/O.
  `submission._record_submission_locked` locks participant/challenge with joined
  events; PostgreSQL can lock joined rows too. Confirm actual SQL lock targets
  and coordinate final scoring with submission commits. Adding an event lock
  locally without checking the opposing order can deadlock.
- **Deadlines and overdue states.** The scheduler is serial, polls and can be
  delayed by provisioning or restart. Reuse bounded/resumable provisioning; a
  process heartbeat does not prove a task or deadline progressed. Start currently
  checks the lower start bound but can activate registration after `event_end`.
  Do not force that overdue state through ACTIVE just to send start/end alerts
  or invent `registration -> ended`. Keep a truthful refusal/recovery policy.
  Pause never extends the end time or implies stopping VMs; any time change
  belongs to existing authorized schedule/lease reconciliation.
- **Submission and read authority.** Keep the half-open competition window
  `event_start <= now < event_end`, ACTIVE state, participant, release and
  prerequisite checks in `challenge._access` and again at submission persistence
  after slow verification, including signed-receipt admission. Define the
  serialized admission instant against concurrent pause/end. No API-only or
  browser-clock check. That helper also gates hints, ratings and downloads;
  preserve separate finished-event read policy. ENDED means results available
  under existing scoreboard publication/freeze and principal read policy, never
  automatic public disclosure. Temporary-account admission also passes through
  `participant.accounts.live_participant_for_user` and
  `principal_participation.viewing_participant_for_credential`; changing only
  the scoreboard would not establish post-event viewing.
- **Registration is not one operation.** `public_registration` gates published
  self-registration; organizer `participant.lifecycle`/`bulk_import` intentionally
  allow late additions, while `participant.accounts` and
  `principal_participation.admit_service_participant` have different checks.
  A deadline exception for an authorized live-event addition must not permit
  additions after ended/cancelled/archived. Preserve capacity, moderation and
  tenant policy in the owning services rather than applying a universal
  “registration state only” rule that breaks the existing late-add contract.
- **Resource cost and authority.** Use `services.range.{tasks,batch,provision}`,
  `ctf.bridges`, `_event_range_lease.reconcile_event_range_leases`,
  `event.get_cleanup_time()` and CMS `expire_due_ranges`. Cancellation must fence
  already-running launches and reconcile participant/spare resources; cancelling
  only pending tasks cannot establish that. `pack_references` explicitly counts
  pending/running launches separately from terminal events. Keep pack admission,
  capacity, credential ceilings and verified teardown; no direct provider call
  or CMS/Engine model import. `auto_cleanup=False`, archive and a completed task
  do not authorize unbounded leases or prove cleanup. Preserve review delay.
- **Archive surface.** `archive_event` exists, but the organizer serializer and
  interactive action map omit archive. If delivery scope requires an organizer
  action, extend those existing surfaces with the same live lifecycle authority,
  audit and schema gates; do not make generic status PATCH writable. Do not
  silently expose new UI/API functionality as part of the #528 alert change.

## Extensibility and evidence boundaries

The extension seam belongs at the existing lifecycle service invocation: manual
policy admission or trusted scheduled task/claim, authored deadline occurrence,
and observed aware time. Carry the smallest existing typed arguments/references
needed to validate those facts; do not invent a generic transition-context JSON
schema, pluggable workflow engine or durable permission grant. A future
reschedule, second scheduler replica or additional delivery channel must use
that same guard and existing claim/adapter policy. Define occurrence identity
independently of task retry/replacement; explicitly handle a schedule moved away
and back rather than assuming a mutable timestamp is a revision identifier.

Maintained evidence owners are `tests/ctf/test_event_lifecycle.py`,
`test_event_lifecycle_api.py`, `test_prepared_event_mutations.py`,
`test_unified_authorization.py`, `test_drf_api_token_access.py`,
`test_authorization_operation_map.py`, the scheduler handler/claim/concurrency
suites under `tests/ctf/test_services`, submission/receipt/access/hydration suites,
and the existing #528 communication/organizer suites. Evidence must distinguish
all eleven permitted edges, every forbidden pair and the conditional source/time
cases; task execution, state commit, final scores, durable declaration, transport
acceptance, read and verified cleanup are distinct assertions. PostgreSQL
transactional evidence is required for competing claims, joined-row lock order,
submission/end races and post-commit crash recovery; SQLite/mocked callbacks
cannot establish those properties. Include revoked/limited credentials,
account-only/cross-tenant placement, deleted events, early run-now, reschedule,
paused deadline, overdue start, retry exhaustion and handoff failure.
Follow ADR-019's boundary-mock policy: use real first-party lifecycle, policy,
hydration and scoring behavior, substituting only supported external/process
boundaries. Do not add internal-function patches or expand legacy baselines to
make the new tests pass. Feature-boundary changes also pass ADR-022's handbook
coverage gate at `docs/technical/architecture.md`.

For subsequent implementation, retain the required full ADR guard and relevant
native checks from `AGENTS.md`/`.gc/plan-rules.md` (Python/import checks, API/client
drift, and deployment validators only where touched). This preflight changes no
runtime code, tests, schemas, requirement status or trace links. It does not
authorize new state transitions, pause-duration accounting, automatic activation
after expiry, a new archive UI, new infrastructure, a new notification channel,
private scenario logic, or broader identity/scoring/retention redesign.
