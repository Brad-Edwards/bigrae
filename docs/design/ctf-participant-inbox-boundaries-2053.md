# Participant inbox and content boundaries (#2053)

Architecture preflight for CTF-008 and CTF-014, inspected 2026-10-29 at
`9c5c5856a`. This specializes ADR-051; it is guidance for implementation, not
an implementation plan or evidence of shipping behavior. ADR-040, ADR-036,
ADR-065 and ADR-066 retain their compatibility, security, retirement and
activation boundaries. Application paths below are relative to
`shifter/shifter_platform/`.

## Existing owners and material gaps

| Concern | Canonical incumbent and consequence |
| --- | --- |
| Inbox API and authority | `ctf/api/communication_inbox.py`, `ctf/api/urls.py` and `ctf/services/communication/inbox.py` already supply event-qualified list/detail/read/acknowledge. Extend these; do not create another participant notification API or reuse organizer permissions. |
| Persistence and identity | `ctf/models/communication.py` and `communication_delivery.py` own immutable revisions, occurrences, snapshots and one receipt per snapshot. `communication/release.py` commits in-app availability with the admitted occurrence. No message store, repository wrapper, outbox or workflow framework is missing. |
| Receipt state | `ParticipantReceipt` and `interact_with_receipt` already preserve first read/acknowledgement timestamps. Durable dismiss state, its projection and action are absent. Closing a toast in `CtfWorkspaceLayout.tsx` is only local presentation state. |
| Safe content | `ctf.communication_contracts` owns admission and digests; `MarkdownContent.tsx` already has the closed `ctf-communication-markdown/v1` parameter. Admission currently validates image destinations without rejecting image syntax, and `MAX_RENDERED_BYTES` is declared but unused. Removing media while rendering does not satisfy rejection at admission. Regex URL checks do not prove equivalence to Markdown/browser parsing. An unknown runtime profile currently selects the general renderer; the TypeScript literal type is not a runtime gate. The existing `markdown-it-py` dependency and `ctf/content_images.py` demonstrate CommonMark token parsing; reuse that parsing approach, not its broader image-copy admission policy or fetching workflow. |
| Browser and realtime | The shell's `useEventNotifications` still expects content-bearing announcement/challenge frames. `notification/realtime.py` now emits `communication` references or `refresh`. No participant inbox hook/page, durable dismiss UI, reconnect recovery or cross-tab receipt convergence exists. Reuse the shell and query client, adapting their current protocol rather than restoring legacy payloads. |
| Retention and caching | `communication/retention.py` physically purges expired campaigns; inbox queries do not independently apply that cutoff. Inbox responses currently have no explicit private/no-store policy. List pages include full bodies, so a finite row limit alone is not a response-byte or rendering-cost bound. |

## Identity, authority and interaction semantics

The API's `message_id` is the **recipient snapshot UUID**, not the campaign,
revision, transport notification or event UUID. The existing unique intent key
distinguishes campaign/occurrence/range generation; the unique
`(intent, participant_public_id)` snapshot and one-to-one receipt collapse a
retry. Equal content in two occurrences remains two inbox entries. Never
deduplicate by subject, digest, email, user alone or campaign.

Every list, detail, continuation and receipt action resolves the live session
and exact parent event through `participant_for_inbox`/`inbox_for_participant`.
Require the event, live participant public identity and snapshot user identity
together before slicing or disclosing content. Workspace membership, organizer
authority and knowledge of a UUID grant no access to someone else's inbox.
Rejoining with a new participant identity must not recover an old identity's
messages. Team changes do not retarget an admitted snapshot.

Keep `viewing_participant_q`, not the scoring/competition predicate: a
disqualified viewer can manage their own receipts without gaining play rights.
Ban, removal, account deactivation/deletion and deleted event deny access.
Retained access after event completion or workspace archive is separate from
competitive play and never extends a temporary account's lifetime or bypasses
its forced-password-change gate. `participant_for_credential` is an incumbent
for principal participation, but its active/paused-event, time and competition
checks are not the historical inbox policy. ADR-066's prepared principal/scope
columns and policy adapters do not authorize an independent S8 activation here;
future authority changes belong at the existing CTF service/shared credential
boundary, with no OR of old and new grants or fabricated Django users.

Read, dismiss and explicit acknowledgement are independent monotonic facts on
the existing receipt. Extend its action seam with a closed operation choice,
preserving existing routes, rather than adding combinations of Boolean flags
or a second receipt model. The service owns server timestamps and idempotency;
repeat/concurrent actions keep the first timestamp and cannot erase another
action. Dismiss hides an item from the default presentation without deleting
content, marking it read or acknowledging it. Dismissed required items must
remain reachable with their unmet obligation visible. Acknowledgement is a
deliberate action allowed only by the pinned `explicit` policy. Do not backfill
new state from delivery, existing timestamps or historical aggregate counters.

GET, prefetch, pagination, toast display and socket writes never change receipts.
An intentional message-open interaction may POST read after authorized content
loads; GET itself stays read-only. Preserve the event → participant → receipt
locking and live rechecks in `interact_with_receipt` against lifecycle removal.
Receipt changes do not call release, enqueue delivery or invoke transport I/O.
Required acknowledgement uses existing accessible buttons/dialogs with clear
labels, keyboard operation and focus restoration; it never blocks help, range
or terminal access, logout or emergency navigation. An inaccessible/unsupported
body cannot be represented as successfully reviewed or acknowledged by the UI.

## Cross-cutting gates

| Layer | Required reuse and satisfaction |
| --- | --- |
| HTTP authentication | Keep `_drf_settings.py` bearer-first fail-closed authentication, `IsAuthenticatedSession`, `InboxView` and `SessionCommunicationSchema`. Valid API tokens, service credentials and invalid bearer plus valid cookie must not become participant receipt sessions. Reuse `config.session_credentials`/session middleware; unsafe session actions require CSRF. `CTFAccountBoundaryMiddleware` admits paths, not object authority. Never widen its participant prefix to admit admin work. |
| Parsing and public contract | Reuse `CommunicationJSONParser`/`ClosedJSONParser`, `ClosedSerializer`, `validate_query`, explicit inbox serializers and `CommunicationSchema`. Reject duplicate JSON keys, unknown fields, repeated query keys and malformed types/Unicode with bounded errors. Actor/event ownership and timestamps are server-derived. Current query limits are 1–100 rows and offset 0–100000; content has separate 200-code-point subject, 65,536 UTF-8 source-byte and 131,072 rendered-byte bounds. Extend serializers and generated `openapi/v1.json`/`frontend/src/api/schema.d.ts` through `manage.py api_contract` and `npm --prefix frontend run gen:api`; preserve ADR-040 compatibility rather than hand-editing types or silently replacing offset contracts. New operations also belong in `ctf/api/operation_policy.py`'s coverage inventory; registration alone is not authorization. |
| Content admission and rendering | Tighten `ctf.communication_contracts` for all HTTP/internal authors; reuse the existing profile in `MarkdownContent` for participant bodies and organizer previews. Reject HTML, media/image syntax including references, embeds, executable/credentialed/IP/localhost URLs and attachments at admission. Render inert text/code/structure only; subjects are React text. Use the Markdown-decoded URL's normalized meaning: fragment or unambiguous same-origin path, otherwise exact deployment-allowlisted HTTPS host. Cover entities, escapes, encoded controls/backslashes, protocol-relative links, reference links, GFM autolinks and host/port normalization with shared adversarial fixtures. No raw HTML, `rehype-raw`, `dangerouslySetInnerHTML`, second renderer, URL fetch or content-image ingestion. Enforce rendered bytes as well as source bytes and bound pathological parsing/rendering work. Unknown profiles fail closed instead of falling into the general renderer. |
| Browser policy and content policy | `config/_browser_security.py` and the SPA host's nonce handling remain authoritative under ADR-036; keep enforced CSP, same-origin connections and current referrer policy. CSP does not enforce allowed navigation hosts, nor does its `img-src` permit communication images. Project any needed normalized link-host policy from server configuration through the authenticated API; never infer it from authored content, branding, caller options or a Vite environment variable. Empty policy permits no external links. The settings hostname-shape validator is not proof of URL safety (it can accept localhost/IP spellings rejected by content policy). Render under current policy when an allowlist tightens without mutating an immutable revision. |
| Lifecycle and privacy | Reuse `communication/lifecycle.py`, participant lifecycle services, `communication/retention.py` and `prune_ctf_communications`. A released item survives cancellation of future/unclaimed work. Retention uses the latest target-event end and existing typed window; scheduler lateness, delivery retry expiry, socket replay TTL and content retention are different clocks. Apply the owning retention decision consistently to list/detail/actions, including purge lag/races; missing or purged items return the same opaque unavailable response. Never recreate a receipt from a retained wake-up. Keep physical erasure, encrypted coordinates via `shared.field_encryption`, and existing `PROTECT` relationships. Participant projections need no coordinate decryption and expose no other recipients, addresses, provider receipts/errors or raw source documents. Use Django's cache-control mechanisms with private/no-store for inbox content/errors; the SPA shell's `never_cache` does not cover API responses. |
| Errors and evidence | Reuse `CTFCommunicationError` → `ctf.api._base.ctf_error_response` → `shared.api.errors`: opaque 404 for unavailable parents/items, 400 for invalid input, 409 for policy conflict, 429 with retry guidance and 503 for dependency failure. Explicitly map new domain codes; the fallback is 503. Never return `str(exc)`, rejected content/keys or raw detail dictionaries. Reuse `RequestIDMiddleware`, `config.logging.ECSFormatter`, `shared.log_sanitize`, body-free `shared.audit` and `communication/metrics.py`'s fail-soft closed labels. Sanitization prevents log injection, not disclosure: no bodies, subjects, URLs with secrets, recipient data or exception dumps in logs/audit/metrics. Keep strict release audit atomic and truthfully separate transport acceptance, socket write, read and acknowledgement. No new receipt journal or per-recipient metric labels. |
| Configuration and runtime | This inbox needs no new secret, worker or env control. Reuse `_ctf_communication_settings.py`'s typed policy, `_product_env.py`'s alias-conflict gate, `_channels.py`, `_cache_settings.py` and `_redis.py` with existing TLS/AUTH. Any changed binding must pass `_env_manifest.py` and generated `env-manifest.json` (`name/default/source_file`), `shifter/installation/{schema,loader,render,runtime_inventory*}.py`, `scripts/gcp/render_runtime_env.py`, Helm `values.schema.json`/templates, GCP overlays, Compose and supported AWS bindings. Portal, scheduler and worker consume the same policy; do not duplicate host lists in browser build configuration. |
| Host, secrets and architecture | Keep `config/asgi.py` origin → session → credential revalidation → temporary-account boundary → consumer composition and existing workload/SQL/NetworkPolicy controls. No message, delivery coordinate, cookie or token in URLs/query strings, argv, shell interpolation, task metadata, environment dumps, frontend bundles or Terraform state. Inbox delivery needs no cloud job or additional egress/service. If job environment transport is changed, `shared.cloud.sensitive_env.split_env` and Kubernetes job admission are additional mandatory gates, not an excuse to inline secrets. `.importlinter` and `scripts/check_layer_imports/layer_imports.yaml` keep cross-domain access at public service facades; never import workspace/Engine models or copy their validators into CTF. |

## Recovery, pagination and extension seams

Keep `shared.notifications`, `SharedNotificationConsumer` and
`notification/realtime.py:publish_communication_wakeup`: subscription and every
replay/fan-out recheck live topic policy; recipient-specific groups and payload
projection prevent broadcast content disclosure. The envelope's `event_id` is
the **snapshot replay identity** for a communication wake-up; the `topic`
identifies the parent event. Do not use that envelope field to choose the inbox
event. Validate the bounded frame/reference shape, then invalidate the authorized
query; neither a reference nor a topic grants item access. Legacy `refresh`
frames remain generic hints. Socket delivery state is transport replay evidence,
not a participant receipt; a second tab may receive no replay at all.

Build on `frontend/src/api/client.ts`, TanStack `queryClient.ts`, generated types,
`CtfWorkspaceLayout`, `features/ctf/routes.ts`, `frontend/src/router.tsx` and
`ctf/urls.py`/`shared.spa_host`. Browser requests stay same-origin cookie/CSRF.
Several existing CTF query keys omit event identity and participant lookup helpers
can choose the first event: neither pattern is safe for this inbox. Use an
explicit event selection and principal/session-qualified, event-qualified
list/detail keys using non-secret identity/context, never a cookie or session
key. Resolve a selected/deep-linked event at the server, without
changing another tab's ambient event or using workspace context as participation.
An event discovery projection, if needed, belongs to the CTF participant service.

Reconnect, online return, tab focus and bounded polling must reconcile list and
receipt state from the API even when sockets are disabled or frames lost.
`queryClient.ts` globally disables focus refetch, so convergence needs explicit
inbox query options; no new global event bus is needed. Cross-tab signalling,
if used, carries only invalidation references and never becomes authority.
Mutations keep the shared no-auto-retry policy, retain the exact message/action
for explicit retry and reconcile uncertain responses. No offline mutation queue,
persisted browser message store or local timestamp as server truth.

Clear private content, toasts, pending mutations and continuations on account,
session or event changes and authorization loss; cancel in-flight reads and
ignore responses from the old context. Do not show stale privileged data on a
403/404 or carry previous-page placeholders across scope changes. Offline state
must be visibly stale and must not claim a receipt was committed. A bounded
refresh rechecks authority; already disclosed content cannot be recalled from a
disconnected browser.

Preserve deterministic `released_at`/snapshot ordering and merge by `message_id`.
Offset pages can shift under new releases, dismissal and retention deletion:
deduplication alone cannot prevent skipped entries. Refresh/reset continuation
on reconciliation so the full authorized list converges, with finite page/query
and aggregate response/render budgets. Do not fetch all messages to calculate
unread counts or render every retained body. If stronger continuation guarantees
are needed, extend the canonical page contract compatibly with an opaque,
bounded continuation tied to the event/filter; reauthorize every page rather
than treating a cursor as authority. No new generic pagination framework.

Future rich-content variations use the existing pinned profile/version seam,
shared conformance fixtures and closed renderer parameter. A future receipt
action extends the same closed receipt operation. A future transport extends
the existing adapter contract without changing message identity or receipts.
CTF-014's branding, gettext/i18n and trusted app extensions remain separate:
none supplies HTML, link policy, a renderer plugin or executable message actions.

## CTF-014 customization boundaries

ADR-051-R11 and the [communications preflight](../architecture/ctf-communications-raes-inject-preflight-2047.md)
already own these distinctions. `ctf.extensions.register_flag_validator` and
`register_scoring_strategy` support deployment-installed, trusted Django apps.
Their process-wide callable registrations are neither tenant plugin admission
nor a sandbox or a general challenge-type contract. Do not generalize them to
communication profiles, recipients, receipt actions or transport handlers, or
put inbox work in `AppConfig.ready()`.
Signed scoring evidence in `shared.receipt_validation` is also separate from
communication `ParticipantReceipt`; neither can satisfy the other's policy.

Branding remains the existing `CTFEvent` projection and organizer validation
(`logo_url`, bounded hex `theme_color`, description). If the inbox shows event
branding, reuse that projection and the canonical `contentImages.tsx` first-party
image resolution; a logo never admits images inside a message. Do not add CSS,
template paths, renderer options or link-host policy to message data. Django
templates are deployment-owned files rendered with escaped data. Never compile
organizer bodies, descriptions or subjects as Django template source, apply
`mark_safe`, or expose request/model objects as message context. The separate
`ctf.services.email_template` placeholder-only contract remains the owner of
existing email wrappers; it is not a second message format or an inbox renderer.

Django UI/template translation already uses `LocaleMiddleware`, `USE_I18N`,
`LOCALE_PATHS`, `locale/` and the Dockerfile's build-time `compilemessages` step;
reuse `tests/config/test_i18n_configuration.py`. Do not describe those settings
as absent. React currently has no gettext/catalog integration and both SPA HTML
hosts declare English: middleware alone does not translate React literals.
This issue must not claim multilingual SPA support or add a CTF-local catalog,
translation editor, service or browser translation library. Any future SPA
catalog delivery belongs to the shared Django/gettext integration under the
same authenticated API, cache and CSP boundaries.

`MessageRevision.default_locale` is stored, but authored variants and locale
selection are not implemented in the current authoring or inbox contracts.
Keep the current default content usable without inventing locale fields here.
If variants are separately added, use that immutable revision/profile seam:
normalize and bound tags and variant count/aggregate bytes centrally, validate
every variant with the same content policy, and bind locale/content/profile to
the revision digest. The existing ADR selection rule is exact tag, base language,
then that revision's default; never another revision or automatic translation.
Locale is presentation, never authority or a new occurrence/recipient/receipt
identity. Product strings stay in Django catalogs; authored messages stay data.

## Evidence required and non-goals

Extend existing `tests/ctf/test_communication_{inbox,contracts,serializers,lifecycle}.py`,
delivery/occurrence/retention and PostgreSQL concurrency suites, shared notification
and real ASGI tests, `MarkdownContent.test.tsx`, query/shell tests and ADR-055's
Playwright/axe lanes. Evidence must cover two events on one account, other
recipients/parents, removal/rejoin and temporary accounts, simultaneous receipt
actions/removal/purge, distinct occurrences versus retries, email-only exclusion,
policy-change/unsupported content, stored and reflected XSS through the real
admission/API/UI under enforced CSP, lost/duplicate/out-of-order wake-ups,
offline/reconnect/two-tab convergence, moving pages and emergency navigation.
Include unknown-profile rejection and template-expression inertness; neither
branding nor template/i18n configuration may relax the communication profile.
Unit rendering or passing CSP alone does not prove admission or browser safety.

Reuse `.ground-control.yaml`, `.gc/plan-rules.md`, `AGENTS.md`, ADR registry and
exceptions, `.github/quality-path-filters.yaml`, `.pre-commit-config.yaml` and
`Makefile`/`frontend/package.json` checks. Run the full CI-level ADR guard for
architecture changes; implementation adds native Python/import/API-contract and
SPA/browser/accessibility checks as applicable. Update the existing
`ctf-communications` user/technical documentation and coverage entry for actual
behavior; do not create a second feature entry or claim completion from guidance.

This issue does not redesign campaign authoring, audiences, scheduling, delivery
leases/retries, email/PLAT-103, RAES/range ingress, credential issuance, customer
scope or S8 authorization, or implement challenge/scoring extensions or platform
localization. No chat, attachments/media pipeline, HTML sanitizer
stack, service worker, plugin/channel registry, external rendering/analytics
service or new hosting is needed. Do not revive ADR-065's retired writers,
invent historical receipt success, turn acknowledgement into a play/lifecycle
gate, or make WebSocket availability a prerequisite for durable inbox access.
