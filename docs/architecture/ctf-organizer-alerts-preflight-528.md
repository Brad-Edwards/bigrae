# Organizer lifecycle and late-range alerts (#528)

Architecture preflight, 2026-10-30. Scope: CTF-1002 and the automatic start/end
alerts from CTF-1004 included in #528. This is guidance for implementation,
not an implementation plan or evidence that alerts work. The maintainer
decisions of 2026-10-30 add an organizer in-app inbox to the email channel.
The [design](ctf-organizer-alerts-design-528.md) records the result.

## Existing behavior and design boundary

Pre-event scheduling, configurable lead time, throttled dispatch, and recovery
already exist. Build on `CTFEvent.range_spinup_minutes` (default 30, range
0–1440), `get_spinup_time()`, `ctf.services.event.scheduling`,
`CTFScheduledTask`, and `ctf.services.range.{tasks,batch,provision}`.
The current event lifecycle uses `REGISTRATION`, `ACTIVE`, and `ENDED`;
the supplied trace description's `SCHEDULED` is not a new event state.

The missing capability is organizer delivery. All four functions in
`ctf.services.notification._organizer` return `channel_unavailable`.
ADR-071 supplies email transport through the communication worker, but
`RecipientSnapshot`, `DeliveryCommand`, audience resolution, worker eligibility,
and inbox receipts still assume participant recipients. A nullable participant
foreign key exists for historical deletion; it does not support organizers.

[ADR-065-R3](../adr/065-retire-legacy-ctf-notification-writes.md) remains closed
until that entire recipient path is supported. Extend the existing CTF ledger
with a closed organizer-principal recipient case for these server-authored
alerts on the in-app and email channels. Preserve participant identity and receipt invariants. Do not add
a second outbox, delivery worker, notification store, or generic recipient
framework. This is a specialization of ADR-051, not revival of legacy writes.

## Recipient and authority contract

- Reuse `event_notification_organizer_ids` in `ctf.services.event.staff` for
  existing human delivery-candidate projection. Its source identities are
  `owner_principal_uuid` and live co-organizer principal assignments. Deduplicate
  by event and principal, never by address. Moderators, judges, all users with a
  global role, and platform administrators are not implicit subscribers.
- Candidates are not permission grants. ADR-066 and
  `ctf.services.unified_authorization` supersede legacy owner/group/superuser
  shortcuts. Resolve live human principals through `management.services` and
  the shared principal port; check current native event policy for disclosed
  information. Lifecycle facts require `event.read`; range counts retain the
  monitoring surface's `event.manage_ranges` boundary. Use the shared
  authorization request/target/scope contracts for internal recipient checks;
  never manufacture a recipient's browser session or reuse a service's
  responsible contact as its human identity. A service owner without eligible
  human co-organizers has no email recipient: record that bounded disposition.
- Keep automation authority separate from recipient eligibility and initiating
  provisioning credentials. Only trusted lifecycle/deadline sources may create
  these system notices. `AdmissionActor(system=True)` is an internal capability,
  not a request field or a missing-actor fallback. Interactive communication
  authoring retains live `CredentialContext`, exact communication scopes,
  credential ceilings, and `event.manage_communications` admission.
- Use `workspaces.services.resource_scope_from_ids` and
  `resolve_resource_scope`, plus CTF's existing credential-scope resolver, for
  exact account/organization/workspace ancestry and active resource state.
  Account-only placement is valid; missing workspace must not imply installation
  scope or cause a fabricated workspace. No new scope JSON or local ancestry
  validator belongs in this feature.
- Freeze the authorized recipient identity and encrypted coordinate at release.
  Recheck live eligibility before transport, including retries and reclaimed
  claims; policy outage is unavailable, not permission or an empty audience.
  Ownership transfer, staff revocation, deactivation, and deletion suppress
  unsent work and erase coordinates through the existing lifecycle/retention
  owners. Never retarget an old command to a new owner/address. An in-flight
  external effect cannot be recalled; document that existing at-least-once limit.

Organizer identity must be explicit in the existing closed contracts and
database constraints, including uniqueness per occurrence/event/principal and
recipient-kind field combinations. Extend `ctf.communication_contracts`,
model validation, release, worker fencing, reporting, and retention together.
Keep participant-only invariants for participant rows, including immutable
`participant_public_id`; do not put principal UUIDs in that column. An
organizer in-app row uses the shared per-snapshot receipt. Participant inbox, list/detail,
receipt, preview and reporting queries must never expose organizer content or
coordinates, even when the same human also participates in that event. Existing
campaign reports need their own live authorization; a recipient label is no grant.
The organizer case is system-only in this scope. `ctf.api.communication_schema`
derives public alternatives from the shared audience vocabulary: extending that
vocabulary must not accidentally enable interactive organizer targeting. Preserve
closed public schemas and regenerate OpenAPI/client artifacts only if their
supported contract changes, using the existing drift/compatibility gates.

## Timing, readiness and durable handoff

`run_ctf_scheduler` claims one due task at a time and executes serially.
`provision_event_ranges_throttled` can occupy that executor past event start.
Heartbeats prevent stale recovery but do not let `EVENT_START` execute. Deadline
assessment must have bounded latency while provisioning continues: reuse
bounded/resumable work and existing task coordination, not a new scheduler,
timer thread, polling browser, or an assumption that another replica will help.
Retain claim tokens, conditional settlement, coalescing under the event lock,
idempotent participant assignment, shutdown/resume, and stale recovery.
The spin-up task's current heartbeat update is not claim-token-qualified;
do not treat it as proof of ownership for a new effect or continuation.

At the current event-start deadline, assess the registered provisioning cohort
through the existing CTF range/status service boundary. Reuse
`get_provision_progress` as the count projection owner and the participant query
rules as the roster owner; reconcile their present all-participant queries with
the requirement's registered cohort in those owners. Do not copy a new roster
filter into notifications. Explicitly account for removed/banned participants,
observers, team/shared assignment policy, and registration during a spin-up.
Count participants, not range objects, requests, attempts, or spare ranges.

Provisioning request success, scheduler completion, and range readiness differ.
Use the assigned range's authoritative CMS status through `ctf.bridges`, the
existing `range_status_changed` synchronization in `ctf.signals`, and the status
service's reconciliation boundary. CMS already retains ordered status delivery
in `RangeStatusDelivery` through `cms.handlers.ctf_bridge`; reuse that owner
through the CMS facade, without CTF importing its models or adding another
status queue. Cached `ready` without a current assignment
is not readiness. Missing assignment, pending dispatch, provisioning, error and
unknown status all need explicit treatment; missing/stale evidence cannot count
as ready. Avoid per-participant synchronous provider calls at the deadline.
Record total, ready and not-ready counts with observation time; empty cohort
means no delay alert. Failure/capacity refusal is distinct from work still in
progress. Never send raw `errors` lists or capacity/provider assessment payloads.

Late readiness is checked at scheduled start even if activation is blocked;
start/end alerts describe successful automatic transitions only. Continue
authorized provisioning during `ACTIVE` without bypassing cancellation,
current credential checks, capacity admission, or server-owned range leases.
Mail failure must not stop provisioning, undo a committed transition, retry
the lifecycle action, or postpone event start until every range is ready.

Use existing event lifecycle services and communication
`create_system_event_campaign`/release/scheduling owners for recoverable handoff.
Persist the declaration with its owning transition/deadline decision, or retain
recoverable work through existing task/intent persistence. A post-commit callback
alone is lossy. Today start/end callbacks run only when the transition returns
true: a crash after commit but before the callback loses the alert on retry.
Recovery must observe/materialize the same occurrence even after the event has
changed state. Transport runs in the existing communication worker, outside
domain locks; channel outage/backpressure retains bounded recoverable work and
truthful disposition rather than silently dropping it or claiming delivery.

Occurrence identity pins event, automatic transition/deadline kind and the
authored schedule occurrence. It must survive scheduler task replacement,
mutable retry `scheduled_for`, restart and repeated checks. A reschedule fences
unreleased old deadline work; pause/resume is not another automatic start.
Recheck the current schedule and event state at execution (the current start
handler has no fresh start-time guard; the end handler does). Distinguish
scheduled deadline from actual observation on overdue startup; do not invent
historical readiness from today's state. Use aware UTC instants and the existing
event-time validators. Grace/lateness and expiry are bounded server policy.

## CTF-1004 lifecycle and deadline boundaries

The #528 implementation adds organizer alerts; it does not authorize a lifecycle
rewrite. Reuse `ctf.enums.EventStatus`/`VALID_TRANSITIONS`,
`event._lifecycle`, `event.scheduling`, and `CTFScheduledTask`. The requirement's
historical `scheduled`/`completed` trace descriptions mean `REGISTRATION`/`ENDED`;
task `COMPLETED` and communication delivery state are separate facts. Existing
templates and mocked callback tests do not establish organizer delivery.

The following are current design risks to preserve or resolve explicitly in the
owning services when the affected behavior changes, not extra #528 features:

- **Clock and submission authority.** The scheduler polls every 30 seconds by
  default and can be delayed by serial work, restart or database outage. It
  cannot promise an exact wall-clock transition. Submission fairness must remain
  enforced in `challenge._access.assert_challenge_available_for_participant`,
  called before verification and again by `submission._record_submission_locked`.
  Reuse its participant, event, release and prerequisite gates; no API-only,
  browser-clock or scheduler-only substitute. CTF-1004's end cutoff implies
  `event_start <= now < event_end`; the current helper rejects only
  `now > event_end`, so equality needs explicit coverage if timing is changed.
  That helper also governs hints, ratings and downloads; preserve the separate
  read-only post-event policy. Revalidate after slow verification before score
  persistence, with a defined admission instant for a request crossing the
  deadline. Use aware instants, Django's UTC/`USE_TZ` configuration and existing
  event serializers/model time ordering; `event_timezone` is presentation, not
  a second schedule. Do not add another date parser or validation vocabulary.
- **Concurrent transition authority.** Reload current event time, status and
  deletion state under the event lock before an automatic effect, coordinating
  with the still-current task claim and authored occurrence. Fenced task
  settlement alone does not fence a handler's state change or alert declaration.
  `activate_event` locks its event, but `complete_event` currently trusts the
  supplied instance and `_transition_event` does not lock it. The end handler's
  fresh time check outside that mutation is insufficient against concurrent
  rescheduling/cancellation. Keep event-first lock order and avoid locking a
  task then waiting for an event. State/schedule-task persistence must commit
  together at its existing owner. Do not bypass `CTFBaseModel.full_clean()` or
  `ctf.signals` model-access invalidation with a raw status update. Activation
  must retain `content_hydration.assert_event_content_hydration_ready`.
- **Refusal and overdue execution.** A false lifecycle result currently lets the
  executor mark a task completed; distinguish already-applied work from hydration
  refusal, stale schedule and a terminally ineligible state. The state machine
  has no `PAUSED -> ENDED` transition, and registration can survive a blocked
  activation past event end. Record those dispositions truthfully; do not force
  `ACTIVE`, reopen submissions, send a successful start/end alert, or silently
  add transitions to make overdue work succeed. Pause-at-end and restart after
  both deadlines need explicit lifecycle policy before any broader correction.
- **Completion versus its effects.** `complete_event` saves `ENDED` before
  leaderboard recomputation, lease expiry and participant results handling.
  An exception there can prevent the organizer callback; retry then sees an
  ended event and skips those effects. Preserve recoverable declaration/work
  at the owning commit, independent of whether a replay changes state again.
  Reuse `scoring.recompute_event_leaderboard` and existing result-notice identity;
  organizer mail retries must neither recompute scores nor repeat teardown.
  Do not infer an automatic transition merely from current `ACTIVE`/`ENDED`:
  manual actions and resume can produce those states too. Likewise, the existing
  `event_lifecycle` communication admission checks current status, not historical
  transition evidence; recover the committed occurrence through the existing
  trusted notice/admission seam rather than weakening its guard for all campaigns.
- **Cleanup is a lease boundary.** Reuse `get_cleanup_time()`,
  `services._event_range_lease.reconcile_event_range_leases`,
  `ctf.bridges.cms_reconcile_ctf_range_leases` and CMS `expire_due_ranges`.
  Event end is not proof of resource destruction. Preserve review delay,
  credential lifetime ceilings, participant/spare leases and verified cleanup.
  A scheduler end must not impersonate an organizer to call destructive range
  services, bypass leases, or treat `auto_cleanup=False` as permission for an
  unbounded resource lifetime. Legacy cleanup tasks already delegate to CMS.
- **Transition evidence.** Existing lifecycle messages do not distinguish the
  automatic source. Record a successful change with previous/next state, task
  and stable occurrence identity, scheduled/observed times and a system source;
  record refusal/replay separately. Reuse module loggers and the shared audit
  event/system-actor contracts when retaining audit evidence, never a fake
  organizer or a second transition-log model. Logs and audit must not contain
  recipient coordinates, content, flags, credential material or raw exceptions.

Required evidence extends the maintained lifecycle, challenge-access/submission,
hydration, scheduler claim/concurrency, lease and communication restart suites:
exact endpoints; slow verification crossing end; simultaneous manual/automatic
end and cancellation/reschedule; blocked/paused/overdue states; and crashes after
state commit or an end effect but before alert declaration. PostgreSQL evidence
must cover event/submission lock order and stale claims. Callback invocation,
task completion, committed transition, cleanup completion and backend acceptance
are distinct assertions. Keep trace descriptions truthful; this preflight does
not change requirement status or certify historical `IMPLEMENTS`/`TESTS` links.

## Cross-cutting gates and canonical owners

Paths below `ctf`, `shared`, and `config` are relative to
`shifter/shifter_platform/`.

| Layer | Reuse and required satisfaction |
| --- | --- |
| HTTP/authentication, if touched | Canonical `ctf.api` controllers, `shared.api.principals.authenticated_credential`, session/CSRF and token authentication, `ctf.services.actors` and native authorization. Preserve `ctf.api.operation_policy`'s command/scheduled/external effect inventories: intrinsic start/end automation does not borrow the creator's later credentials; interactive lifecycle/task controls require `event.manage_lifecycle`; communication release retains its admission owner. No new unauthenticated callback, public system flag or recipient-address input. The organizer inbox adds session-only, event-scoped routes. Organizer email needs no HTTP endpoint. |
| Parsing and validation | `ctf.communication_contracts` owns closed audience/trigger/channel/content shapes; explicit DRF serializers and `shared.api.closed_serializer.ClosedSerializer` delegate semantics there. Preserve `shared.api.strict_json` ingress and unknown/duplicate-key rejection for any new HTTP shape. `CTFBaseModel.full_clean`, immutable fields and database constraints cover internal writes; bulk writes bypass model validation and need service checks and constraints. Merely changing a serializer or enum is insufficient. |
| Persistence and concurrency | Existing campaign/revision/intent/snapshot/`DeliveryAttempt` models, atomic release with strict audit, deterministic event-first locks, worker leases and pre-I/O fences. Native policy I/O is prepared before SQL locks, followed by local credential/placement fencing. Extend migration, uniqueness, deletion and purge contracts without fake historical recipients. |
| Transport and errors | Existing `ChannelAdapter`, reference-only `DeliveryCommand`, `DeliveryOutcome`, `adapters.email`, `shared.email.attempt_email` and `shared.email_deadline`. One bounded attempt, one retry owner, existing budgets/backoff and availability check. Ledger acceptance, backend acceptance and read remain separate; unavailable/empty/partial are truthful outcomes. No direct SDK, process-local email executor, or provider-specific alert code. |
| Content, secrets and OS exposure | Pinned validated revision, trusted escaped email wrapper, safe subject/header rules and deployment-owned link allowlist. `EncryptedStringField` owns coordinates; only the email adapter decrypts. Entry point secret hydration uses existing field-key/email secret references. No credentials, PII, message bodies, or provider text in argv, task metadata, manifests, shell tracing, audit, logs or metrics. No new mail service or data destination is approved by this note. |
| Error envelopes and observability | Reuse `CTFCommunicationError`/`CTFError`, canonical CTF API mapping and `shared.api.errors`. Fixed authored errors and closed reasons; `safe_user_message` bounds text but does not redact it. `logger.exception` can leak nested provider/parser text. Reuse `config.logging.ECSFormatter`, its allowed extra fields, `shared.log_sanitize`, communication metrics and strict `shared.audit`. Capture due/observed times, counts, lag, unavailable/suppressed/failed outcomes and correlation IDs. Use closed metric labels, never event/recipient IDs or addresses. Process heartbeat, task heartbeat and alert delivery are separate health facts. |
| WebSocket, if touched | `shared.notifications`, its topic/payload validators, live subscription/replay authorization, ASGI origin/account boundaries and reference-only wake-ups. Existing `ctf:event` admits participants and is not an organizer confidentiality boundary. Default event fan-out broadcasts to participants too. Do not use it as the organizer alert transport or equate replay with durable delivery. |

## Configuration and runtime estate

No new process, secret or deployment knob is inherently needed. Keep lead time
on the existing validated event field. Any necessary timing/batch parameter
belongs in the existing scheduler or communication policy owner, with numeric
bounds and timeout/lease/shutdown relationships; reuse
`config/_ctf_communication_settings.py` and product environment alias handling.
Do not duplicate the meanings of lead time, dispatch throttle, notification
grace, transport timeout and retry deadline.

Configuration changes must satisfy `config/_env_manifest.py` and generated
`env-manifest.json`, `config/_email.py`/`_runtime_env.py`, installation
`runtime_inventory*.py`, `scripts/gcp/render_runtime_env.py`, and the matching
`scripts/bootstrap` renderers. A parsed setting alone is not a deployed setting.
Helm `values.schema.json` has closed shapes; use existing chart helpers, runtime
ConfigMap/secret references, worker identities, database duties, restricted pod
security, `/tmp` probes, NetworkPolicy/provider egress and OpenFGA connectivity.
Sender availability in settings alone does not prove permitted network delivery.
Keep communication workers separate from provisioner Job-launch privileges.
Provisioner admission/env allowlists and `shared.cloud.sensitive_env.split_env`
remain independent gates if that boundary is touched: `EMAIL_API_KEY` is not
classified sensitive by the current suffix rules and must never become a literal
Job env value. Alerting should not change provisioning inputs at all.

For launch/probe changes, inspect compose, the shared Helm chart, installation
runtime inventories, `scripts/bootstrap/aws_eks.py`, the GCP control-plane/rollout
scripts and their startup tests. Historical preflights refer to EC2 launchers
and `platform/k8s/gcp/base`, which are absent in this checkout; use the current
chart and bootstrap owners. Keep `.importlinter`,
`scripts/check_layer_imports/layer_imports.yaml`, `.ground-control.yaml`,
`.pre-commit-config.yaml`, quality path ownership and ADR enforcement intact.
Repository operations use `.ground-control.yaml`'s canonical repository;
historical issue links do not override it.

## Extension seam, evidence and non-goals

The extension seam is the existing closed recipient/audience contract and
server-authored occurrence normalization. Parameterize alert kind, event,
occurrence and bounded count evidence there so another operational alert can
reuse release, worker and transport. Capacity/failure callbacks remain distinct
and unavailable until supported; this issue does not authorize broader capacity
telemetry disclosure. The organizer inbox reuses the existing inbox service,
receipts and safe Markdown rendering. It needs no dynamic plugin or new
content/template system.

Implementation evidence belongs in the maintained notification, scheduler
handler/claim-fence/concurrency, range/status, communication contract/model,
admission/delivery/lifecycle/retention and native-authority suites. Preserve the
negative participant-broadcast tests. Exercise the real scheduler under long
spin-up, zero lead time, unknown readiness, overdue startup, rescheduling and
restart after transition commit. PostgreSQL tests must prove simultaneous
release/revoke/transfer/reclaim and stale settlement, not only sequential mocks;
reuse the email restart/deadline tests and mock external transport/policy only
at their owned boundaries. Inspect output/logs/envelopes for data leaks. The
existing mocked start/end callback assertions do not prove delivered alerts.

Non-goals: implementing this requirement in preflight; rewriting provisioning,
capacity, scheduler, tenancy or policy engines; new queues or hosting; arbitrary
addresses/webhooks; pack-specific logic; credential notices; reviving
`CTFNotification` writes or retired APIs; exactly-once email; re-provisioning on
alert retries; or marking either requirement delivered from guidance. Keep
private scenario data out of fixtures and repository metadata. Run the required
repository ADR guard for this documentation change; implementation must also
run the affected stack, contract and runtime checks specified in `AGENTS.md`.
