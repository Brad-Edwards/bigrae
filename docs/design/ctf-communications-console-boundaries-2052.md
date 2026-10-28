# CTF communications API and console boundaries (#2052)

Architecture preflight for CTF-008/010/012, inspected 2026-10-27 at
`4b44a4285`. This specializes ADR-051; it is design guidance, not an
implementation plan or a claim of shipping behavior. ADR-052, ADR-065 and
ADR-066 retain their authority and activation boundaries. Read the companion
[scheduler preflight](../architecture/ctf-multi-event-communications-preflight-2052.md)
for independent schedule preservation, task/intent divergence and range-lifecycle
boundaries. Application paths below are relative to `shifter/shifter_platform/`.

## Existing owners and remaining gaps

| Concern | Canonical incumbent and consequence |
| --- | --- |
| HTTP | `config/api_urls.py`, `ctf/api/urls.py`, `ctf/api/organizer/communication.py` already expose workspace collections, create, detail, revision, release/schedule and cancel. Extend this surface; do not build another admin API or revive legacy notification controllers. |
| Domain and persistence | `ctf/models/communication.py`, `communication_delivery.py`, `ctf/services/communication/` and `CampaignDraft` own campaigns, revisions, intents, snapshots, attempts and receipts. Django ORM transactions are the persistence boundary; no repository layer, DTO family, outbox or workflow framework is missing. |
| Preview/reporting | No communication preview route exists. `CommunicationCampaignSummarySerializer` exposes campaign status and target count, without revision content/identity or event/channel delivery aggregates. These are missing authorized projections over existing facts, not missing workflow entities. |
| Console | `frontend/src/api/ctfCommunications.ts` and `features/ctf/admin/MonitoringPage.tsx` submit single-event in-app announcements, preserve campaign identity after failed release, page campaigns and cancel. Extend this common path for multi-event composition, preview and status. |
| Delivery | `communication/release.py`, `scheduling.py`, `delivery.py`, `lifecycle.py`, `backpressure.py` and `adapters/contract.py` already own admission, timing, leases, retries, fencing and pressure. Here `communication/` means `ctf/services/communication/`. Only in-app is registered; email remains #1525/PLAT-103. Existing email settings do not prove adapter readiness. |

## Authority, preview and admission

Every discovery, preview, detail, revision, release, schedule, cancel and report
must authorize the complete selected or persisted target set. Reuse
`communication/admission.py:reauthorize`, `lock_authorized_campaign`,
`ctf.services.authorization.resolve_event_authority`, and `workspaces.services`
with `USE_CTF_COMMUNICATIONS`. One current campaign has one immutable workspace
binding. A platform administrator cannot cross workspaces in one campaign or
bypass workspace admission. Do not fabricate membership to make a request pass.

The current event decision is owner, applicable live staff capability, then
audited platform-admin override. Moderators have notification capability;
judges do not. Use the existing role map, including co-organizers, rather than
another role table. Organizer group membership, workspace membership, `is_staff`,
token scope, selected UI context and participation are separate facts. Removed,
demoted, inactive and temporary accounts must not retain organizer authority.

Use `events_with_capability` for discovery and
`communication/selection.py:visible_campaigns` before counts/pagination, then
recheck live authority before disclosure. An event filter cannot expose part of
an unauthorized campaign. All persisted targets, including deleted targets,
determine scope. Missing, wrong-workspace and unauthorized targets produce the
same opaque denial, without a per-target failure list or recipient-count oracle.

Preview reads an existing immutable `MessageRevision` and the campaign's validated
target/audience/channel/trigger policy. Return revision identity and the existing
digest, safe content and bounded event-qualified counts. Reuse
`communication/audience.py:resolve_recipients` and `viewing_participant_q`, not
competition eligibility or email/user-based deduplication across events. Explicit
participant/team references never resolve outside the authorized parents.

Preview creates no intent, snapshot, delivery attempt, receipt, scheduler task,
backlog reservation, socket publication or email. Do not call release and roll
it back. Shared abuse counters and body-free audit are separate from delivery
effects. The current resolver materializes its entire queryset: checking its
length afterwards is not a query-cost bound. Extend its bounded query/count seam
instead of duplicating audience policy in a controller. Counts are current
estimates, not frozen recipients or reserved capacity. Canonical release creates
final snapshots, at due time for schedules, after live authorization/eligibility
and capacity checks. Explain changes since preview; no preview-token store or
second frozen-audience workflow is needed.

Send/schedule from preview must supply its explicit `revision_id`, verified as a
child of that campaign. Default-latest release could send another tab's edit.
Preserve campaign plus `occurrence_key` across uncertain release responses;
existing replay checks reject changed meaning. Draft creation lacks release's
idempotency guarantee: reconcile a lost create response before creating and
releasing a replacement. Never create a new campaign just to retry release.

Choose atomic admission across all target events and requested channels. Invalid
authority, scope, revision, source, dependency or capacity must leave no partial
committed delivery work. Channel/source readiness belongs at the owning service
boundary for every caller, not solely in browser controls or an HTTP controller.
Do not drop a channel/event or substitute in-app for email. Delivery after
admission can partially fail and must preserve that evidence.

## Cross-cutting gates

| Layer | Required reuse and satisfaction |
| --- | --- |
| Authentication and scopes | Keep `config/_drf_settings.py` bearer-first authentication, `shared.api.principals.active_actor_user`, `CommunicationView`, `HasActiveCTFActor` and shared `require_scope`. Invalid bearer cannot fall back to a valid session. Preview/read uses exact `ctf:communication:read`; author/release/cancel uses exact `ctf:communication:write`, with no implication between them. A POST preview explicitly declares read scope through the shared permission metadata: the inherited view maps unsafe methods to write. Unsafe session requests still require CSRF. Never move content or recipient selectors into URLs to avoid CSRF. |
| Authority evolution | ADR-066's account/scope columns, `shared.credentials`, `shared.identity_scope`, `shared.authorization`, `ctf.services.unified_authorization` and `ctf/api/operation_policy.py` already establish the future seam. Keep operation coverage, but do not activate S8 through this issue, OR legacy authority with OpenFGA, interpret null workspace as installation scope, invent another principal/scope contract or fabricate a service's Django user. At coordinated S8, the owning shared credential/action/scope boundary replaces legacy authority, including queued-work checks. Prepared authentication/schema support alone does not prove service-principal support here. |
| Parsing and schema | Reuse `CommunicationJSONParser` / `ClosedJSONParser`, `ClosedSerializer`, communication serializers and `validate_query`: reject duplicate JSON members, unknown fields, repeated query parameters and malformed Unicode/types with bounded authored errors. Preserve separate 1 MiB envelope, 65,536 UTF-8 body-byte, 100-target, 5,000 explicit-reference and finite page bounds. Character limits are not byte validation. Actor, token, origin, authority, system, generation and early release privileges are server-derived. |
| Domain and content | `ctf.communication_contracts` and `enums_communication` own closed audience/trigger/channel/acknowledgement semantics and digests for HTTP and internal callers. Add missing invariants there. Preview uses canonical `MarkdownContent.tsx` with `ctf-communication-markdown/v1`, no raw HTML/media/attachments and deployment-allowlisted HTTPS or safe same-origin links under existing CSP. Its host list defaults empty: any needed projection is server-owned, never caller policy or a broader Markdown profile. Preserve the 131,072-byte rendered limit separately from source bytes; no server URL fetch or external renderer is needed. |
| Concurrency and persistence | Reuse admission's live actor/token association and scope checks, workspace archive/membership mutex, event locks in primary-key order before campaign/revision mutation, existing constraints/immutable fields, nested-savepoint replay handling and strict audit. Model `full_clean` does not cover bulk writes. Delayed human work never becomes system work when its actor disappears. Shared rate/backlog budgets remain in `backpressure.py`; no process-local limiter, Redis workflow truth or second queue. Transport I/O remains outside database locks. |
| Privacy and retention | `shared.field_encryption.EncryptedStringField` protects coordinates; preview/reporting needs no decryption. Return bounded aggregates by default and only explicitly authorized minimal recipient detail if required, never email/coordinates, raw users, provider errors/receipts or another event's roster. Private responses stay out of shared HTTP caches. `communication/retention.py` physically erases content and coordinates; account/event deletion must respect retained `PROTECT` relationships rather than bypassing them. |
| Errors and observability | `CTFCommunicationError` → `ctf.api._base.ctf_error_response` → `shared.api.errors` owns authored errors and request-ID envelopes. Preserve opaque 404, invalid 400, conflict 409, throttle 429/`Retry-After` and dependency 503 distinctions; map new codes there, without a second exception hierarchy. Never forward `str(exc)`, model/parser/provider text or arbitrary detail dictionaries. Reuse `RequestIDMiddleware`, `shared.log_sanitize`, `config.logging.ECSFormatter` and `communication/metrics.py`'s closed labels. Sanitizers prevent log injection, not disclosure; bodies, titles, rejected values, rosters, secrets and coordinates stay out of logs/tracebacks/metric labels. Metrics are fail-soft. |
| Audit | Reuse `communication/audit.py`, existing release audit and `shared.audit` for strict per-target request-attributed evidence; extend bounded campaign/intent/revision references where needed, without a new journal. Required-audit failure rolls back the owning mutation. Global audit is not a tenant history API: trustworthy emitted scope and the registered read gate must precede tenant exposure, never guessed campaign filtering or installation-audit access. |
| Browser and socket | Reuse `frontend/src/api/client.ts`, generated `schema.d.ts`, TanStack `queryClient.ts` and existing dialog/form/error components. Browser credentials remain same-origin session cookies and CSRF, without bearer storage. Mutation retries stay explicit. Key queries/pending submissions by workspace/event selection and revision, refresh preview on edits and clear private state on context/account changes. Preserve focus, labels, keyboard access and loading/empty/denied/error recovery through ADR-055's existing browser matrix. `shared.notifications` retains origin/session/temporary-account/topic checks and reference-only wake-ups; polling/reconnect reads the durable inbox without changing receipts. |
| Config and environment | No new secret, worker or environment control is needed. Reuse `_ctf_communication_settings.py`'s typed bounds/relationships, `_product_env.py` alias-conflict gate, `_email.py`, `_channels.py`, `_cache_settings.py`, `_redis.py` and fail-loud runtime settings. A changed binding must pass `_env_manifest.py` / generated `env-manifest.json` (`name/default/source_file`), installation schema/loader/render and `runtime_inventory*.py`, `scripts/gcp/render_runtime_env.py`, Helm `platform/charts/shifter/values.schema.json` and templates, GCP base/overlays, Compose and supported AWS runtime bindings. Portal, scheduler and delivery worker must see the same validated policy; preserve shared Redis TLS/AUTH. |
| Host and secret exposure | Keep supervised management commands/heartbeats and existing workload security/SQL/NetworkPolicy/admission boundaries. No body, credential or coordinate in task metadata, argv, command interpolation, environment dumps, ConfigMaps, frontend bundles or Terraform state. Email secret references remain references until established runtime hydration. If job env transport is touched, `shared.cloud.sensitive_env.split_env` and Kubernetes job admission are additional gates: `EMAIL_API_KEY` currently matches neither the explicit sensitive names nor sensitive suffixes, so forwarding it as literal job env would leak it. This console needs no such transport. |

## State, extensibility and implementation boundary

One absolute-time campaign has one aware UTC instant, with an explicit display
zone and each event's existing `event_timezone` for local display. A browser
`datetime-local` value alone cannot choose an event zone or resolve daylight-saving
ambiguity. Reuse trigger UTC validation, lateness/expiry and session-only run-now
policy. Rescheduling cannot rewrite an accepted occurrence; the scheduler note
records the current event-time-edit hazard separately.

Campaign/intent state and HTTP 202 describe admission, not delivery. Report by
occurrence/event/channel from snapshots, attempts and receipts, avoiding join
multiplication between channels and receipts. Scheduled audience estimates differ
from released counts. In-app availability, socket publication, email backend
acceptance, participant read and explicit acknowledgement remain distinct. Cancel
stops unclaimed work; claimed work can be suppressed before I/O but concurrent
acceptance cannot be recalled. Preserve mixed outcomes even on a cancelled
campaign. At-least-once transport and stable identity do not promise exactly once
external delivery.

The extension seams already exist: closed audience/trigger vocabulary and resolver,
immutable content profile/revision, channel `DeliveryCommand` / `DeliveryOutcome`,
scoped query projections and operation/action inventory. Parameterize the common
console operation by target-event list and channel policy within one workspace;
single-event announcements use the same input with one event. Future locales
belong to revision data, separate from gettext/branding. No plugin registry,
provider-specific console workflow or new policy evaluator is needed.

Implementation evidence belongs in existing `tests/ctf/test_communication_*`,
workspace/token/authority tests, PostgreSQL admission/delivery concurrency tests,
frontend communication tests and `e2e/a11y/matrix.ts`. Cover moderator success,
judge denial, removed staff, cross-event/workspace and deleted targets, lost
authority between preview/release/due time, concurrent edits, ambiguous responses,
channel unavailability, aggregate cardinality, preview query bounds, audit failure,
time zones and keyboard/error recovery. Prove preview produces no delivery state
or external effect. SQLite cannot prove lock/fencing behavior; reuse PostgreSQL,
and real ASGI/Redis tests when sockets change. Do not mock first-party services.

`CommunicationSchema` derives closed variants from serializers/domain vocabulary.
Pass ADR-040 compatibility and regenerate through `manage.py api_contract` /
`npm --prefix frontend run gen:api`, never hand-edit contracts or generated types.
Preserve operation coverage, ADR guard, `.importlinter`, per-symbol layer checks,
Ruff and frontend gates. Infrastructure/workflow changes additionally pass their
existing native validators. Keep user/technical docs honest about readiness;
this note does not complete the umbrella requirement.

Non-goals: implementing the feature in preflight, email transport, WebSocket
infrastructure, RAES/range-trigger activation, recurring schedules, credential
issuance/resend, arbitrary addresses/HTML/attachments, cross-workspace campaigns,
S8 activation or deployment. Password issuance stays with `participant.credentials`;
organizer-only alerts cannot use fabricated participant rows. Do not dual-write
or reopen ADR-065's retired notification writes, infer receipts from legacy
`sent_count`, reuse the Engine outbox/process-local email executor as a queue,
create another schema/exception/workflow store, import workspaces models or branch
on private pack identity. Keep the maintenance fence and synthetic evidence; no
new hosting, data transfer or external processing is needed.
