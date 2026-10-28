# CTF API integration boundaries (#2052 / CTF-012)

Inspected 2026-10-27 at `4b44a4285`. Architecture guidance only: no implementation
plan, runtime change or delivery claim. ADR-051/052/065 and ADR-066's coordinated
S8 activation boundary govern. Application paths below are relative to
`shifter/shifter_platform/`. The [current communication contract](../technical/shifter_platform/ctf-communications.md)
describes shipped source behavior; the [scheduling preflight](ctf-multi-event-communications-preflight-2052.md)
covers the same issue's event-rescheduling and task-progress hazards.

## Decision and existing owners

Extend the existing CTF API, communication ledger and console. The supplied
`ctf/urls.py` / `ctf/views/api/` trace links are legacy locations. Canonical routes
are `config/api_urls.py` -> `ctf/api/urls.py` ->
`ctf/api/organizer/communication.py` under `/api/v1/ctf/`.
Create/list/detail/revise/release/schedule/cancel already exist, including
multi-event targets. Missing capabilities are authorized preview, richer
event/channel progress and a multi-event console; they need projections and
composition over the existing domain, not another workflow owner.

| Concern | Canonical incumbent to extend |
| --- | --- |
| Authoring, schemas and validation | `CampaignDraft`, `ctf/services/communication/campaigns.py`, `ctf/communication_contracts.py`, `ctf/enums_communication.py`, `ctf/api/serializers/communication.py`, `CommunicationSchema`. Immutable content revisions and closed audience/trigger/channel/acknowledgement vocabulary already exist. |
| Authority and discovery | `communication/admission.py`, `selection.py:visible_campaigns`, `ctf.services.authorization.resolve_event_authority`, `events_with_capability`, `ctf.services.event.staff`, and public `workspaces.services` operations. Here `communication/` abbreviates `ctf/services/communication/`. |
| Audience and execution | `communication/audience.py:resolve_recipients`, `viewing_participant_q`, `release.py`, `scheduling.py`, `lifecycle.py`, `delivery.py`, `backpressure.py`, `CTFScheduledTask` and `DeliveryCommand`/`DeliveryOutcome`. Django ORM transactions are persistence; no repository abstraction is missing. |
| Console and cross-cutting services | `frontend/src/api/ctfCommunications.ts`, `features/ctf/admin/MonitoringPage.tsx`, shared `apiFetch`/`ApiError`, TanStack Query, existing Dialog/form/error primitives and `MarkdownContent.tsx`; `ctf.services.audit`, `communication.audit`, `shared.audit`, `shared.log_sanitize` and `communication.metrics`. |

## Contract clarifications

- **Complete-target, atomic admission:** every discovery, preview, read and
  mutation checks every selected/persisted event. One campaign currently has
  one workspace. Missing, deleted, cross-workspace or unauthorized targets deny
  the whole declaration opaquely; never silently drop them, return per-target
  authorization failures or expose their counts. Apply complete-target
  visibility before pagination/counts. An event filter does not narrow campaign
  authority. Parent-scope every revision, team and participant lookup. Requested
  channel/source readiness and capacity also belong in service admission, not
  just the HTTP controller. Subsequent delivery may partially fail; admission
  must not partially commit.
- **Authority facts stay distinct:** the current platform-admin event override
  does not bypass the separate workspace membership/archive gate. Do not create
  membership to make it pass. Central policy already grants notifications to
  moderators/co-organizers and denies judges; removed staff and loss of the
  organizer role must revoke delegation. Scopes, `is_staff`, workspace roles,
  creator attribution and UI capabilities are not event authority.
- **Preview pins meaning, not future membership:** preview an explicit existing
  immutable revision plus the campaign's fixed targeting/channel/trigger policy.
  Reuse its digest, audience resolver and safe renderer. Return bounded authorized
  counts with an observation time, without creating intents, snapshots, commands,
  receipts, tasks, reservations or transport effects. Never release and roll back
  to simulate preview. Reuse distributed abuse limits, and bound query work before
  materializing recipients: the current resolver builds a list before release's
  audience cap is applied. Actual snapshots remain release-time facts, including
  due-time release. Membership changes can change preview counts; an exact frozen
  preview-equals-send audience would require a separate domain decision.
- **Reviewed send and uncertain retry:** submit the explicit reviewed `revision_id`
  so a concurrent edit cannot substitute latest content. Preserve existing v1
  optional-field compatibility for other clients. Retain campaign/occurrence
  identity across deliberate retries; conflicting replay is a conflict, not new
  work. The current pending hook reference disappears on reload, and draft
  creation does not have release's idempotency guarantee. Recover/reconcile
  server-owned draft/declaration state after an ambiguous response rather than
  creating another campaign or persisting bodies/credentials in the browser.
- **Time and cancellation:** one campaign has one aware UTC instant, even across
  events in different zones. Display the chosen zone, interpreted instant and
  existing `event_timezone`; `datetime-local` plus browser `Date` conversion does
  not resolve event-zone or daylight-saving ambiguity. Reuse the trigger validator,
  release grace/expiry and session-only early-release policy. Cancellation stops
  unclaimed commands; committed claims end the recall guarantee. Pre-I/O
  suppression is additional protection, not recall. Already committed inbox
  availability and accepted external effects remain truthful after cancellation.
- **Reports and audit are evidence:** project per-occurrence/event/channel
  aggregates from snapshots, delivery commands and receipts. One participation
  in each of two events remains two recipients; do not deduplicate by user/email.
  Avoid join multiplication and retry-counter inflation: `DeliveryAttempt` is one
  mutable command per snapshot/channel, not one history row per physical send.
  HTTP 202, scheduler completion, in-app availability, channel acceptance,
  read and explicit acknowledgement are different facts. Preserve mixed,
  unavailable, zero-recipient, expired and retained/purged-history meanings.
  Aggregate counts need no recipient roster or coordinate decryption.

The existing per-event authority audit alone is not a correlated campaign,
revision, schedule, cancel and outcome trail. Extend emission through the existing
audit owner with bounded references, strict mutation/outcome evidence and trusted
request/actor correlation. The deployment-global audit API cannot become organizer
history by filtering historical actor/entity IDs or granting installation audit
access. Customer-visible history requires trustworthy resource scope at emission
and a registered scoped read contract through the audit owner (ADR-066), preserving
integrity and strict rollback. Do not infer scope for old rows or add another ledger.

## Cross-cutting layers the design must pass

| Layer | How the intended design satisfies its canonical gate |
| --- | --- |
| Authentication and browser boundary | Reuse `config/_drf_settings.py`, platform `ApiTokenAuthentication` and owning credential validation, bearer first with no invalid-bearer session fallback. Current human token calls resolve through `active_actor_user`, not `request.user`. `HasActiveCTFActor` and service policy reject temporary/ineligible organizer actors; CTF path-prefix admission is not authority. Preserve `CTFAccountBoundaryMiddleware`, live profile/password-change gates, session CSRF and session-only participant receipts. |
| Exact scopes and future policy | Shared `require_scope` enforces/publishes exact `ctf:communication:read/write`, with no broad event/play substitution or write-implies-read. A POST preview must explicitly publish/enforce its read scope while retaining session CSRF; inherited unsafe-method policy otherwise selects write. Register added operations in `ctf/api/operation_policy.py` and its coverage tests. Its prepared `event.manage_communications` mapping is not live enforcement. At S8 reuse `PrincipalRef`/`ResourceScope`, `CredentialContext`/`CredentialCeiling`, `authenticated_credential`, `ctf.services.credential_scope`, `workspaces.services` ancestry and `shared.authorization`; no fabricated User/workspace, null-means-installation shortcut, or logical OR of policy engines. This issue does not activate S8 or claim service-principal support from prepared schemas. |
| Bytes, query and domain shapes | Reuse `CommunicationJSONParser`/`ClosedJSONParser`, nested `ClosedSerializer` and `validate_query`. Reject duplicate/unknown fields, non-object/deep/nonfinite JSON, malformed Unicode/types, repeated singleton query keys and unsupported media. Keep independent 1 MiB envelope, 65,536 UTF-8 body-byte, 100-target, 5,000 explicit-reference and finite report/page bounds. Domain validators remain authoritative for internal callers; actor/token/origin/system/generation/early-release privilege are server-derived. |
| Content and browser execution | Reuse `validate_message_content`, canonical digest and `MarkdownContent`'s pinned communication profile under existing CSP. The generic Markdown profile is broader. Host allowlists are deployment-owned; omitted browser hosts safely disable external links and do not prove preview/delivery parity. No raw HTML/media, template execution, URL fetching, second renderer or CSP relaxation. Navigation policy does not replace SSRF validation if a future feature fetches URLs. |
| Persistence and concurrency | Reuse live original actor/token checks on release, replay and due execution, workspace archive/membership mutexes, deterministic event-first locks, campaign/revision serialization, database shape/uniqueness constraints and nested-savepoint replay handling. Preserve atomic intent/snapshot/receipt/command/strict-audit writes and no I/O under locks. `full_clean`/`ImmutableFieldsMixin` do not protect every association or bulk/queryset write. Keep `backpressure`, `shared.rate_limit.consume_fixed_window`, fail-closed cache behavior and PostgreSQL backlog serialization; no process-local limiter or missing-actor system fallback. |
| Secrets, retention and host exposure | `EncryptedStringField`/`shared.field_encryption`, `communication.retention` physical erasure and participant removal/deletion hooks remain owners. Credential issuance stays volatile in `participant.credentials`/`shared.credential_delivery`, never immutable content. No bodies, rosters, coordinates, credentials or provider payloads in task metadata, browser persistence, logs, audit, metrics, argv, shell text, environment dumps, temp/public artifacts or manifests. This extension needs no new secret, worker or external processor. If job env is later touched, `shared.cloud.sensitive_env.split_env` and Kubernetes admission are independent gates; `_API_KEY` is not a sensitive suffix and cannot be assumed safe to forward literally. |
| Configuration and deployment shapes | Reuse `_ctf_communication_settings.py`, `_email.py`, `_channels.py`, `_cache_settings.py`, `_redis.py` and `_product_env.product_env`, retaining bounded values/relationships and conflicting `BIGRAE_`/`SHIFTER_` alias rejection. No new knob is required here. Any genuinely exposed setting also passes `_env_manifest.py`/generated `env-manifest.json`, installation loader/schema/runtime inventories, runtime render/bootstrap scripts, Helm `values.schema.json`/ConfigMap, and supported GKE/EKS/Compose bindings. Keep secrets in existing private hydration, consistent portal/scheduler/worker policy, shared Redis TLS/AUTH and native supervision/heartbeat; no new host process or provisioner privilege. |
| Errors, audit and observability | Extend `CTFCommunicationError` -> `ctf.api._base.ctf_error_response` -> `shared.api.errors`. Keep authored opaque 404, invalid 400, conflict 409, 429/`Retry-After` and dependency 503; no `str(exc)`, raw provider/model/parser text or arbitrary `details`. Error normalization/log sanitization is not blanket redaction. Reuse request IDs, `shared.log_sanitize`, `config.logging.ECSFormatter`, strict shared audit and fail-soft `communication.metrics` with closed channel/state/reason/count labels, no event/campaign/recipient dimensions. |
| API/UI publication and workflow | Extend serializer-derived `CommunicationSchema`/`PlatformAutoSchema`, generated OpenAPI and `frontend/src/api/schema.d.ts` via `manage.py api_contract` and `npm --prefix frontend run gen:api`. Preserve ADR-040 compatibility and ADR-065 retirements. Use `apiFetch`, `ApiError`/`describeMutationError`, TanStack bounded read retry/no automatic mutation retry and existing accessible dialog/form/error primitives. Scope query/pending keys by workspace/targets/revision and clear private state on authority/context changes. No browser token store, second client or client-side filtering of global data. |

Whole-repo enforcement also includes `.importlinter`,
`scripts/check_layer_imports/layer_imports.yaml`, `docs/adr/index.yaml`,
`docs/adr/exceptions.yaml`, `docs/adr/documentation-coverage.yaml`,
`.ground-control.yaml`, `.gc/plan-rules.md`, `.pre-commit-config.yaml`,
`.github/quality-path-filters.yaml`, `.github/workflows/_quality.yml` and the root
`Makefile`. CTF uses public tenancy/CMS services and shared contracts, not their
models. Historical issue links do not override the configured canonical repository.

## Extension seams, evidence and exclusions

Parameterize the existing client by a validated target list within one workspace;
the single-event announcement remains the same operation with one target. Content
profile/revision, closed audience/trigger vocabulary, service admission context,
channel `DeliveryCommand`/`DeliveryOutcome` and bounded server-owned budgets are
the seams. Group reports by registered channel/state instead of hardcoding an
email/in-app workflow. Future identity/scope variation uses ADR-066's shared
contracts at its activation boundary, not another DTO or local policy evaluator.

Extend existing `tests/ctf/test_communication_*`, token/workspace/authority and
operation-inventory tests, frontend communication/Markdown tests and the browser
accessibility matrix. Required evidence includes all acceptance roles, removed
staff, cross-tenant/complete-target denial, revocation at preview/release/due/replay,
concurrent revisions, no-effect bounded preview, uncertain retries/reload, aggregate
query/cardinality bounds, mixed channel states, post-claim/committed-inbox cancel,
audit rollback, malformed-input/log leakage, time-zone/DST handling, keyboard/focus
and error recovery. Real PostgreSQL proves locks/fences; use the existing Redis/ASGI
lane if socket behavior changes, not SQLite or mocks of first-party services.

Run ADR guard for guidance; implementation also runs affected service/API/frontend,
Ruff/import, generated-contract drift/compatibility and native runtime checks.
Registry validity alone proves neither runtime reliability nor completed CTF-012.

Non-goals: implementing this preflight; separate API/service, repository/outbox/job
framework, cross-workspace broadcast, frozen preview membership, new transports or
provider SDKs, RAES/range-trigger activation, secret messages, recipient export,
second schema/error/audit hierarchy, exactly-once/recall guarantees, cloud IAM or
private-pack changes, S8 activation or deployment. Only in-app is registered;
email remains #1525. Never accept unavailable channels as deliverable, silently
downgrade, revive retired notification writers or reverse the maintenance fence.
