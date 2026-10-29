# Durable CTF email: architecture preflight for #1525

Reviewed 2026-10-29 at `9c5c5856a`. The supplied issue is the contract; this
requirement-free preflight adds guidance only, not an implementation plan.
Repository identity comes from `.ground-control.yaml`. Historical #581/#1095
notes explain transport/template boundaries; their retired CTF send entry points
are not integration targets. ADR-051 and ADR-065 govern this change.

## Decision and ownership

Implement email as a closed `ChannelAdapter` over the existing CTF communication
ledger. `DeliveryAttempt` already **is the durable outbox command**, one row per
recipient snapshot and channel, updated across retries. Do not add an email
outbox, recipient model, broker, scheduler, or workflow owner. Admission commits
the intent, frozen recipients, commands and strict audit together; requests return
202 after that commit without transport I/O. Admission/backlog limits bound this
work; asynchronous transport alone does not bound request work.

`CampaignStatus.RELEASED` and `IntentStatus.RELEASED` mean materialization, never
delivery completion. #1525 also requires **persisted aggregate transitions**;
today `reporting.delivery_report()` only derives counts on reads. Persist the
occurrence's delivery disposition within `CommunicationIntent`, separately from
its release/cancellation lifecycle, derived from its existing delivery rows.
Keep one derivation shared with reporting; no independent announcement counters
or resurrection of `CTFNotification`. Pending commands preclude completion;
all accepted means backend acceptance only; mixed terminal outcomes remain
partial; all failed, cancelled/suppressed/expired, and an empty audience must not
be reported as successful delivery. Preserve channel/event breakdowns and count
recipients separately from channel commands. Settlement, cancellation and expiry
must leave the aggregate consistent or durably recoverable through the existing
worker, including concurrent final settlements and restarts. Changes to mutable
delivery evidence must never rewrite the pinned authored declaration.
Use a documented lock order compatible with event-first release/lifecycle work;
scope worker row locks explicitly rather than locking a whole `select_related`
graph accidentally when adding aggregate updates.

CTF owns recipient/content resolution and maps transport results into the existing
`DeliveryOutcome` vocabulary. `shared.email` owns MIME assembly, sender defaults
and backend execution; it must remain independent of CTF models, policy and queue
state. Extend that transport seam to expose one bounded synchronous attempt with
safe classification and optional receipt; preserve existing best-effort callers'
contracts. Provider-specific handling belongs there and in `config/_email.py`,
not in a CTF provider switch or a new provider framework.

## Canonical incumbents and gates

Application paths below are relative to `shifter/shifter_platform/`; other paths
are repository-relative. `communication/` abbreviates
`ctf/services/communication/`. These are reuse obligations, not claims that
current code already satisfies every email acceptance criterion.

| Layer | Incumbent and required satisfaction |
| --- | --- |
| HTTP authentication and policy | `ctf/api/organizer/communication.py`, shared session/Bearer authentication, `active_actor_user`, exact `CTF_COMMUNICATION_READ/WRITE` scopes, `communication/admission.py`, `workspaces.services` and `resolve_event_authority`. Preserve CSRF for session writes, live actor/token binding, active workspace and complete-target event authority on admission/replay/due release. Email addresses, token ownership and origin labels grant no authority. Do not activate ADR-066's staged identity cutover here. |
| Request/domain shapes | `CommunicationJSONParser`, `shared.api.closed_serializer.ClosedSerializer`, communication serializers, `ctf/communication_contracts.py`, `CTFBaseModel.full_clean()` and database constraints. Reuse their closed fields, sizes, audience/trigger/channel vocabulary and replay conflicts. Bulk writes bypass `save()/clean()`; service validation and database uniqueness still apply. No client-selected recipient coordinates, headers, transport settings or retry policy. |
| Persistence and timing | `communication/release.py`, `audience.py`, `backpressure.py`, `scheduling.py`, `DeliveryAttempt`, `RecipientSnapshot`, pinned `MessageRevision`, and `CTFScheduledTask`. Reuse atomic release, fixed audience and existing attempt identity, skip-locked claims, lease fencing, bounded fairness and shared-storage admission budgets. PostgreSQL is truth; `on_commit` may accelerate work but cannot be its durable handoff. No locks or transactions across provider I/O. |
| Lifecycle and privacy | `communication/lifecycle.py`, `retention.py`, `viewing_participant_q`, `shared.field_encryption.EncryptedStringField`. Recheck eligibility/fences before irreversible I/O, use the captured coordinate rather than a fresh participant/user email, and honor coordinate erasure and physical retention purge. Never retarget a retry. Claimed work cannot be promised recall. |
| Content and MIME | `validate_message_content`, pinned content profile/digest/locale, `shared.email.render_template`, trusted `templates/ctf/email/`, and `ctf.services.email_template`'s scalar placeholder parser. Reuse bounds and escape untrusted values; never compile authored text as Django/Jinja code. Send one recipient per message without shared To/Cc/Bcc lists. Reuse Django address/header validators for sender and recipient, rejecting CR/LF; the Markdown subject validator alone permits newline characters. |
| Settings/configuration shapes | `config/_email.py`, `_runtime_env.py`, `_ctf_communication_settings.py`, `_product_env.product_env`, `_env_manifest.py`; `installation`'s closed backend settings/bundle contract, `runtime_inventory{,_aws,_gcp}.py`, `scripts/gcp/render_runtime_env.py`, `scripts/bootstrap/aws_eks.py` and Terraform email inputs. New knobs must reach supported deployment rendering and pass type/range/cross-field validation, not merely exist in Django settings. Use canonical `BIGRAE_*` product names through the existing conflict-rejecting `SHIFTER_*` compatibility ingress. Regenerate env/bundle artifacts with their existing generators, never hand-edit generated JSON. |
| Secret and OS exposure | `entrypoint.sh`/`entrypoint-lib.sh` hydrate database, field-encryption and optional ESP credentials. Preserve `EMAIL_API_KEY_SECRET_ID` as a reference to JSON containing `api_key`; the value flows through stdin to parsing and runtime process environment only. AWS SES uses workload IAM; GCP uses the configured Anymail backend. No secret/body/coordinate in argv, URLs, shell tracing, environment dumps, manifests, generated config, Terraform state or task metadata. Stable field-key availability across redeploys is necessary to read queued coordinates. No new service or data destination is authorized by this issue. |
| Runtime/infrastructure | Existing `drain_ctf_communication_deliveries` and `platform/charts/shifter/templates/ctf-communication-worker-deployment.yaml`; chart security/tmp/proxy helpers, closed `values.schema.json`, worker service account, database duties, NetworkPolicy, AWS workload IAM and GCP Secret Manager/Monitoring IAM. Prove the worker can read/update the ledger, decrypt coordinates, reach the selected mail API and emit metrics under its actual identity. It needs no provisioner Job authority. Keep the entrypoint, non-root restrictions, disabled automatic token mounting, writable heartbeat directory and digest-pinned image. |
| Errors, logs and audit | `CTFCommunicationError`, `ctf_error_response`, `shared.api.errors`/`shared.errors`, `shared.log_sanitize`, `config.logging.ECSFormatter`, `communication/audit.py` and `shared.audit`. Keep fixed public errors and closed result reasons; validate opaque bounded provider receipts rather than storing provider text. No raw exception/HTTP response/body/address/credential logging. `safe_log_value` bounds text but does not redact it; `logger.exception` includes causes and `full_clean` can log rejected values. ECS drops undeclared extra fields; use its supported correlation fields or deliberately extend the canonical formatter. Audit is evidence, never a queue; its failure cannot roll back an already accepted external effect. |
| Observability and consumers | `communication/metrics.py`, `reporting.py`, communication serializers/schema, and the existing communications console. Retain fail-soft AWS/GCP publication and closed low-cardinality outcome/channel/reason labels. Distinguish queued, claimed, retry, accepted, exhausted, permanent failure and unavailable transport; expose backlog age, recovery and bounded attempt latency. Provider acceptance never updates participant read/ack receipts. Preserve ADR-040 OpenAPI generation/compatibility and generated frontend types if the API changes. |

## Gaps that must not survive email activation

- **Transport observation:** `shared.email.send_email()` currently catches all
  exceptions, logs their tracebacks, returns only a boolean, and ignores the
  count returned by `msg.send()`. It cannot distinguish transient/permanent
  failure, zero sends or provider identity. Calling it unchanged, or using
  `send_email_async()`, cannot settle durable delivery truth. Rendering, malformed
  coordinate/header and configuration failures need explicit safe classification;
  a known permanent error must not become the worker's generic `adapter_error`.
- **Availability:** only in-app is registered today. Gate email registration and
  preview/release readiness on a usable, explicitly configured backend in every
  process. Console/dummy/file backends must not accept production delivery or
  print real content. Test transports remain explicit test substitutes. A
  missing/misconfigured dependency stays visible; it cannot downgrade to in-app.
  Registration is the existing closed mapping, not `AppConfig.ready()` or plugin
  discovery. Existing AWS EKS IAM has no `ses:Send*` grant found in this baseline;
  confirm and supply the narrow selected-backend permission before claiming AWS
  parity. Backend selection alone is not proof of effective permission or egress.
- **Idempotency:** `DeliveryCommand` currently carries `attempt_id` but not the
  row's `idempotency_key`. Resolve the existing row/key through that identity
  with matching intent/snapshot/event/channel bindings; do not mint a key per
  claim, retry, lease or provider call. A stable Message-ID is correlation, not
  proof of provider deduplication. Provider acceptance followed by a crash before
  settlement is inherently uncertain: a lease fences database completion, not
  an external send. Use verified provider deduplication/reconciliation where
  supported and test its retention window against the retry budget. Otherwise
  document at-least-once duplicate risk; do not claim duplicate-free recipient
  delivery. If the acceptance criterion is interpreted as that stronger promise,
  unsupported backends require an explicit capability decision before release.
- **Retry bounds:** `delivery._settle()` enforces attempt/elapsed limits only
  after a retriable result. Repeated claim/crash recovery, or a retry already past
  its deadline, can still reach a transport. Enforce ceilings before I/O as well;
  preserve the first-attempt baseline across reclaim and distinguish maximum
  delivery elapsed time from scheduler lateness and queue age. Current jitter is
  added after capping the exponential term; bound/document total delay. SDK
  automatic retries/throttling must fit the same transport budget, not multiply
  a second retry policy. Protect other recipients and the in-app channel from
  poison work and slow email within the existing worker's fairness policy.
- **Actual timeout and shutdown:** passing `timeout` into an adapter does not
  enforce it. Bound connection/read/SDK retry/throttle time inside the selected
  Django backend; a thread future timeout does not stop its sending thread.
  Keep the entire attempt below lease and health windows with settlement margin.
  The command currently
  finishes a whole serial claimed batch after SIGTERM: 100 calls at the default
  10-second timeout exceed Kubernetes' default 30-second termination grace.
  Align bounded claims, stop behavior and grace; forced death must recover every
  unfinished claim. Heartbeat freshness proves liveness, not queue progress.
- **Decryption and deletion:** ordinary snapshot ORM loads automatically decrypt
  `delivery_coordinate`. `_preflight()` currently joins the snapshot before the
  adapter exception boundary, and `reporting.recipient_rows()` loads whole
  snapshots despite never projecting the coordinate. Defer sensitive fields
  outside the email boundary; decrypt only the one intended coordinate and
  isolate missing/corrupt/erased rows to that command with safe outcomes. A purge
  between claim and settlement must not abort the batch or resurrect work.
  A missing deployment key is a dependency outage, not evidence that every
  recipient has a permanently invalid address.
  Preserve the final lifecycle recheck; `_fence_reason()` does not currently
  check a released intent's range generation. Any supported generation-bound
  email must use its owning service's current-generation fence, not a CMS/Engine
  model import. This does not enable new range/RAES sources.
- **Content stability:** no Python communication-profile renderer currently
  matches the browser's `MarkdownContent` communication mode. Do not substitute
  generic Markdown-to-HTML or `mark_safe`. Escaped source text is an inert option;
  rich email output needs the same no-media/allowlisted-link/byte-bound contract
  with shared adversarial fixtures. Resolve same-origin links using deployment
  `SITE_URL`, never a request Host, and revalidate destinations after resolution.
  Pin the revision and any wrapper variation that changes authored meaning:
  reloading mutable `CTFEmailTemplate` must not change a retry or override its
  subject. The legacy placeholder-only wrapper is not a communication schema.
- **Operational truth:** existing run counters are aggregate across channels,
  `reclaimed` is never populated, and backlog depth omits channels with no rows.
  Verify useful email/recovery attribution and zero/drained observations rather
  than assuming metric names prove them. Persist outcomes before metrics; metric
  outages cannot cause resend. Extend existing reports/logging, not another
  delivery dashboard store. Read-time counts alone do not satisfy the issue's
  persisted aggregate transition criterion.

## Extension seam and boundaries

Future provider variation belongs behind the existing synchronous `shared.email`
transport, selected by `EMAIL_BACKEND`; vary timeout, stable delivery identity
and optional receipt through that seam. Retry/batch/backlog policy stays in the
typed communication settings and worker. Content profile and locale remain
revision data. No provider, source, event or scenario identity may select core
workflow behavior. No new shared exception hierarchy, settings parser, redactor,
audience resolver or generic task framework is warranted.

ADR-065's maintenance fence remains permanent. Use `communication/cutover.py`'s
`resume_migrated_schedule()` with the same campaign/occurrence and lateness policy;
do not automatically resend historical successes, replay every staged notice on
adapter installation, infer historical recipients, or restart retired senders.
Historical aggregates remain historical evidence.

Non-goals are platform-wide migration of workspace invitations/password resets,
durable password/invite-token storage, organizer recipients fabricated as CTF
participants, bounce/open/click webhooks, mailbox/read guarantees, attachments,
localization/theming systems, new RAES ingress, and identity/tenancy redesign.
Volatile credential issuance remains at `participant.credentials`; retrying mail
must never rotate passwords or retain issuance objects. Necessary changes to the
existing transport, worker, ledger outcome and deployment wiring are in scope.

## Required evidence for implementation review

Reuse `tests/ctf/test_communication_{delivery,delivery_postgres,lifecycle,cutover,
metrics,console_api,settings}.py`, `tests/shared/test_email.py`, config email/env
tests, runtime-inventory/renderer tests, chart contracts and `scripts/stack-smoke`.
Exercise real services and PostgreSQL locking; substitute only external mail and
cloud metric boundaries. Existing simulated lease tests are not a process
restart/redeploy proof. Evidence must cover:

- crash before send and after provider acceptance/before settlement; two worker
  processes, stale settlement, recovered work, stable recipient identity and
  backend-specific duplicate limits;
- partial success, zero recipients/sends, permanent and transient failures,
  deadlines already exceeded before I/O, poison coordinates, cancellation,
  removal, generation fencing and retention races; persisted aggregate convergence;
- slow transport under SIGTERM/forced kill, heartbeat and backlog progress,
  bounded admission latency without provider calls, and no secret/content leakage
  through validation, error causes, logs, subprocess argv or console backends;
- actual worker config/secret/IAM/network wiring for AWS and GCP with synthetic
  fixtures; no real email, new service or upload is needed for automated proofs.

Run the repository's full ADR guard. Python/import changes additionally require
Ruff/import-linter; changed charts require Helm/schema, kube-linter and pinned
kubeconform checks; Terraform/workflow changes require TFLint/actionlint as
applicable. Keep user/technical communication docs truthful when email ships;
registry validation is not proof of delivery correctness.
