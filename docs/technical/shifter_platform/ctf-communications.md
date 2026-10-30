# Communication API and receipt boundaries

The `/api/v1/ctf/communications/` controllers translate closed DRF inputs into
`CampaignDraft` and the existing communication services. PostgreSQL remains the
workflow authority; no second outbox, scheduler, or transport layer is added.
See the [API and cutover preflight](../../architecture/ctf-communication-api-cutover-preflight-2100.md)
and [operator guide](../../features/ctf-communications.md).

The [console design (#2052)](../../architecture/ctf-communications-console-design-2052.md)
records the preview, reporting, timeline, and console architecture. The #2052
[API](../../architecture/ctf-api-integration-preflight-2052.md),
[console](../../design/ctf-communications-console-boundaries-2052.md), and
[scheduling](../../architecture/ctf-multi-event-communications-preflight-2052.md)
preflights record the boundaries.

The [email delivery design (#1525)](../../architecture/email-delivery-design-1525.md),
its [preflight](../../architecture/ctf-email-durable-delivery-preflight-1525.md),
ADR-051-R14, and ADR-071 define the email channel on this same ledger and
worker.

ADR-065-R4 adds organizer recipients to this ledger. The
[organizer-alert design (#528)](../../architecture/ctf-organizer-alerts-design-528.md)
and its [preflight](../../architecture/ctf-organizer-alerts-preflight-528.md)
record the architecture and the boundaries.

## Organizer recipients

`ctf.services.notification._organizer` declares four system alerts:
`event_started`, `event_start_refused`, `ranges_not_ready` and `event_ended`.
Each declaration is a system campaign with the internal `event_organizers`
audience and an absolute due time. `schedule_declaration` commits the scheduled
intent and its `RELEASE_COMMUNICATION` task in the caller's transaction. The
lifecycle service declares start and end alerts under the event lock, in the
transaction that changes the state. The campaign UUID derives from the event,
the alert kind and the schedule occurrence, so a replay returns the first intent.
Organizer alerts skip the shared system authoring rate budget at creation.
Release-time admission backpressure still applies. When a declaration is
refused, `declare_or_retain_lifecycle_alert` records the alert in the claimed
task's `pending_alerts` metadata in the same transaction. The handler calls
`redeclare_pending_alerts` and raises `AlertDeclarationPending` while an alert
remains, so the task retries with its existing backoff.

| Boundary | Organizer behavior |
| --- | --- |
| Audience | `{"kind": "event_organizers", "event_ids": [<one event>], "action": "event.read" \| "event.manage_ranges"}`. Only `create_system_event_campaign` and the `system_milestone` origin accept it. `AudienceKind` and the public serializer do not include it. |
| Candidates | `event_notification_organizers`: the owner principal and live co-organizer principals with an active human account. Candidates are not grants. |
| Release | `prepare_organizer_recipients` checks the pinned action for each candidate with `principals_holding_event_action` before locks. Under the event lock, release keeps only prechecked principals that are still candidates. A policy outage reschedules the release within the grace window. |
| Snapshot | `recipient_kind="organizer"`, `principal_uuid`, `user_id` and the encrypted email coordinate. No participant identity. Database constraints enforce the shape and one row for each intent, event and principal. |
| Worker | Before the claim lock, `_organizer_policy_stop` rechecks the pinned action with `principals_holding_event_action`. A denial suppresses with `organizer_policy_denied`. An evaluator outage settles the command as retriable with `authority_unavailable` and no I/O. Under the lock, pre-flight suppresses with `organizer_ineligible` when the principal is no longer a candidate. |
| Removal | Staff revocation, and a change from co-organizer to another role, call `on_organizer_removed`. It cancels unclaimed commands and erases the coordinate. |
| Console | The campaign list excludes organizer campaigns. `authorized_campaign` returns not found for them. |
| Participant inbox | Selects participant rows only. |

The organizer inbox is session-only:
`/api/v1/ctf/events/<event_id>/organizer-alerts/`. It supports list, detail,
`read/` and `dismiss/`. `organizer_inbox` requires a current candidate and
checks each closed alert action with `assert_event_action` on the session
credential. Rows whose pinned action is denied are absent. Organizer alerts
have no acknowledgement. Responses are private and the browser does not store
them.

The scheduler start handler calls `start_event_automatically` and then
`alert_if_ranges_not_ready`. The readiness check uses `get_provision_progress`:
the eligible registered cohort, where `ready` needs a current range assignment.
A refused start raises, so the executor retries with backoff. After the end
time, the automatic start returns `expired` and never activates the event. A spin-up yields
when a different non-spin-up task is due. The requeued run is a continuation:
it uses the time left before start as its window and skips the repeated
capacity admission. Capacity and provisioning-failure organizer callbacks stay
unavailable.

## Email channel

`adapters/email.py` registers `EmailAdapter` in the closed adapter mapping.
`registered_channels()` returns `email` only when `EMAIL_DELIVERY_AVAILABLE` is
true. That setting is false for console, file, and dummy backends outside
development and test runtimes. Release, preview, cutover, and lifecycle notices
read the same value in the web and worker processes.

The adapter reads only the coordinate of its own snapshot. It renders the
pinned subject and Markdown source through the trusted `ctf/email/communication`
template, which escapes the source. It calls `shared.email.attempt_email()` with
the worker transport timeout and the row `idempotency_key`. The key becomes a
stable Message-ID. A missing snapshot or erased coordinate is terminal. An
unreadable coordinate is retriable within the worker budget.

The worker expires a claim before I/O when its attempt or elapsed ceiling is
spent. On SIGTERM it finishes the attempt in flight and releases the other
unattempted claims. When the last transport command of an occurrence settles,
it refreshes `CommunicationIntent.delivery_state` through
`disposition.refresh_after_settlement()`.
Each cycle reconciles pending occurrences whose commands are all terminal.
`WorkerOutcome` metrics carry `Outcome` and `Channel` dimensions, and each
settlement writes one log line with the attempt, channel, status, and reason.

## Console read projections

`ctf/services/communication/reporting.py` owns the console reads. It writes no
row. `read_admission.py` prepares current native authority over the complete
persisted target set before SQL projection, then rechecks local proof and target
state. Direct service calls require an `AdmissionActor` and a campaign UUID.

| Projection | Source | Bound |
| --- | --- | --- |
| Targets | `targets.py`: current `event.manage_communications` on each exact event scope, before offsets | 100 events per page |
| Preview | `recipient_queryset` grouped by event; no row load | Target count |
| Delivery | `DeliveryAttempt` and `RecipientSnapshot` in separate grouped queries, plus the persisted `delivery_state` | 100 occurrences per page |
| Recipients | Snapshots for one occurrence and one event; also needs current `event.manage_participants` | 100 rows per page |
| Activity | Campaign, revision, and intent attribution columns | 100 revisions and 100 intents |

Separate grouped queries keep receipts from being multiplied by channel rows.
The delivery projection adds the latest `release_communication` task status for
a scheduled intent. Preview returns each event's local due time from
`event_timezone`. Native principal and credential attribution identify the
actual revision, release and cancellation actor. Services never inherit a human
contact identity. Historical migration `0065_communication_attribution` preserves
revision-1 authors; `0077` joins both migration histories, and `0078` adds native
revision and cancellation attribution. Strict
`shared.audit` stays the investigator record; it has no organizer read surface.

`_reschedule_event_tasks` cancels only the event-derived task types it
recreates. A pending `release_communication` task keeps its authored due time.

## Authorization and serialization

Native credentials carry the authenticated principal, current proof and action
ceiling through the controller and owning service. Current policy checks require
`event.manage_communications` on every target. Recipient detail also requires
`event.manage_participants` on the selected event. SQL owns exact account,
organization and workspace placement. Legacy user, staff and workspace roles
cannot substitute for policy. Provider unavailability remains a bounded 503.

Policy I/O occurs before domain row locks. Local credential and target checks
fence subsequent projection or commit. Event locks follow primary-key order,
ahead of campaign/revision mutations. Revise and cancel reload the campaign
under lock. Per-target strict audit retains principal, credential, operation,
correlation and authority evidence without message content.

`target-options/` exposes explicit `{kind,id}` parents and authorized event
options. The parent filter accepts zero or one of `account`, `organization` and
`workspace`. Parent names come from a narrow Workspaces projection of scopes
already admitted through event authority; this is not directory access.
The incoming workspace-only `targets/` contract remains a projection of the
same discovery helper. Authorization precedes offsets in both surfaces.

Organizer collection queries filter complete-target authority before pagination,
have finite limits and preload target aggregates. Continuation describes only
authorized campaigns; the organizer page can load each subsequent page.
Authorization must cover the complete persisted target set; hiding a deleted
target must not turn a multi-event campaign into a smaller authorization scope.
Body, envelope, reference-list and page limits are independent. Domain validators
reject unhashable discriminators, invalid Unicode and invalid URL parser inputs
with bounded errors. API errors use authored messages through `shared.api.errors`.

## Inbox and socket policy

The [participant inbox boundary note](../../design/ctf-participant-inbox-boundaries-2053.md)
(#2053) records the preflight constraints, including CTF-014's trusted-app,
branding and Django/gettext boundaries. It does not claim multilingual SPA support.
The [participant inbox design](../../architecture/ctf-participant-inbox-design-2053.md)
records the receipt states, views, content checks, security controls and data flow.

`ctf.services.communication.inbox` matches event, immutable participant public
identity and user together. A committed receipt and released intent prove in-app
availability. Inbox reads exclude campaigns past the retention cut-off from
`retention.expired_campaign_ids`, so purge lag does not extend access. A GET never
creates or changes a receipt. `interact_with_receipt` takes one closed
`ReceiptAction` (`read`, `dismiss`, `acknowledge`). It locks the event, the
participant and the receipt, rechecks live participation, and then sets that
action's first timestamp. No action changes another timestamp. The pinned
acknowledgement policy governs explicit acknowledgement. Retained inbox access
is distinct from permission to compete or temporary-account lifetime.

`InboxFilter` selects the list view. `inbox` hides a dismissed message only when
its pinned policy has no open obligation. `action_required` lists open
obligations. `dismissed` lists dismissed messages. The list adds inbox-wide counts
and the deployment link-host allowlist. Every inbox response carries Django's
never-cache headers (`no-store`, `private`). Validation errors use one generic
`invalid` envelope that does not repeat the input.

Admission parses the body with `markdown-it-py` (CommonMark, tables and
strikethrough). It rejects image tokens, HTML tokens, link targets outside the
policy, GFM `www.` autolinks and rendered output above 131,072 bytes. The link-host
setting rejects `localhost` and IP literals at startup. The participant inbox and
the organizer preview render under the same server-projected allowlist.

The exact `/ws/notifications/` path is admitted for an eligible temporary account
after password change, through the existing origin/session/account stack. The
consumer retains the actual native credential and checks topic policy on
subscription, replay and each delivery. It projects retained payloads
through the current registration. CTF wake-ups contain references only; legacy
bodies become a refresh hint. Revocation at final send retains the undelivered
receipt. The SPA shell refetches the inbox on a `communication` hint and
refetches CTF reads on a `refresh` hint. It drops other frames. The
communication Markdown profile on the canonical renderer drops media and renders
non-allowlisted links as plain text. An unknown content profile shows no body
and offers no read or acknowledge action.

## Public contract and verification

`CommunicationSchema` builds closed object schemas from the actual serializers
and audience/trigger alternatives from the domain vocabulary. Regenerate with
`manage.py api_contract` and `npm --prefix frontend run gen:api`; generated
artifacts are never edited manually. Participant operations advertise only
session authentication.

Behavior coverage lives in `tests/ctf/test_communication_*`, token tests, and the
real ASGI notification tests. PostgreSQL admission tests cover concurrent release,
cancel, removal and scheduler fences. Redis tests exercise fan-out/replay and a
live temporary participant's body-free replay followed by authority revocation.
ADR, import-linter, Ruff, OpenAPI drift/compatibility and frontend checks remain
mandatory. Cutover tests cover historical aggregate preservation, invalid/ambiguous
rows, restart-safe ownership transfer, and database rejection of stale writers.


## Maintenance cutover

This is a maintenance deployment, not a rolling replacement. ADR-065 records the
maintainer-approved legacy write retirement and ADR-040 exception.

1. Disable incoming writes and stop **all** old web and scheduler instances.
   Drain and terminate their process-local email threads. Inspect old workers and
   reconcile every `SENDING` notification and every `RUNNING` task against
   available external evidence. Never guess that dispatch
   succeeded or reset ambiguous work to pending for a resend.
2. Apply migrations with the new code while application workers remain stopped.
   Once a cutover marker exists the fence migration cannot be reversed. It installs conditional PostgreSQL
   triggers; the maintenance command records the permanent activation marker.
3. Run `manage.py cutover_ctf_communications --legacy-producers-stopped --batch-size 100`.
   This assertion is an operator attestation, not automatic process discovery.
   The command checks database quiescence and serializes with in-flight legacy
   writes before committing the fence. It maps at most 100 rows per transaction;
   repeat until `mapped=0`. A failed batch rolls back its campaign mappings and
   task cancellations together. Previously committed batches remain resumable.
4. Invalid content, missing/deleted authority or scope, unsupported targeting,
   missing/duplicate task ownership and orphaned pending tasks block activation.
   Reconcile those declarations explicitly during maintenance. Do not edit a
   rejected row into apparent historical success, truncate its body, drop its
   task, or invent recipients. Failure output contains only bounded codes.
5. Run the same command with `--activate`. All legacy rows must have mappings
   and no pending/running `SEND_NOTIFICATION` task may remain. Historical rows
   keep their original count/status as aggregate evidence, without new recipients
   or receipts. Draft/scheduled announcements become campaigns with their original
   author, target and email channel. Old tasks are cancelled atomically with that
   mapping. Email-unavailable mappings have no runnable release/delivery work.
6. Start only the new workers and web code. Verify old writes return 410, exact
   communication scopes work, participant inbox reads stay parent-scoped, and the
   command's `email_ready` / `channel_unavailable` counts match the deployment.

The PostgreSQL fence rejects legacy row creation and all scheduler claims from
an old binary, including lifecycle and provisioning callbacks. New claims must
declare the ledger writer version in the claim transaction and cutover must be
activated; this declaration expires at transaction end. It cannot recall an already dispatched email; this is why the
process drain precedes fencing. Rollback must be forward recovery using the new
writer and retained fence. Do not reverse migrations, delete the marker, or
restart an old sender. A database restore requires another full maintenance
reconciliation before any workers are admitted.

When email is available, recover one mapped schedule through
`resume_migrated_schedule(legacy_id)`. It uses the original campaign, actor, due
instant and stable `legacy:<id>` occurrence. Retries return the same declaration;
a past-due declaration obeys the existing lateness policy rather than silently
changing its due time. Ordinary migrated drafts remain deliberate authoring work.

Migrated campaigns follow the ledger's retention window. The purge transaction
also physically deletes their legacy source rows and mappings, erasing duplicate
content and preventing later cutover passes from importing it again.

Participant lifecycle notices use stable server-derived campaign identities and
remain visibly staged when email is unavailable. Organizer-only alerts have no
participant audience and remain unavailable rather than creating fake recipient
rows. Password issuance stays volatile in `participant.credentials`; non-secret
login notices never retain passwords, tokens or issuance objects. The deprecated
credential-resend helper fails at the retired dispatch boundary without changing
the participant's password.
