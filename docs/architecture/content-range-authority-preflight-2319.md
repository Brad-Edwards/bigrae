# Content, range, background and streaming authority preflight (#2319)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.


Status: S6 architecture guidance under ADR-066; custom source work remains
subject to #2308's authorization gate. Production authority changes at S8.

Contract: issue #2319 and its dependencies #2314–#2316. No formal requirement
is attached. This note fixes integration boundaries, not an implementation
plan. Paths below are relative to `shifter/shifter_platform/` unless qualified.

## Readiness and observed gaps

Extend the existing services, operations and channels. The following gaps must
be closed before claiming the issue's acceptance criteria:

- `shared.authorization.catalog` currently has `workspace.launch_range` and
  `range.read/manage/access`, but no complete content/source/preparation action
  vocabulary or account-level launch action. `TargetRef` supports installation,
  account, organization, workspace, event and range only. Do not silently map
  customer content administration to `installation.manage_adapters`, a read
  action, or a blanket administrator check to bypass the missing mapping.
- Range scalar account fields already exist, but
  `shared.authorization.model`, `config.openfga_authorization` range ancestry,
  and `engine.services._authorization_inventory` still assume workspace/event
  placement. Schema presence does not make an individual-account range usable
  or delegation inventory complete. Resolve exact SQL-owned parent identity,
  including the actual event association; another event in the same account
  must not supply authority. Reject conflicting/stale parent edges.
- CMS authoring permissions, Mission Control's `actor_user`, content organization
  membership, range owner filters, preparation `requested_by`, and plugin
  organization bindings still contain human/workspace assumptions. At S8 they
  cannot remain a second permission veto on an authorized service administrator,
  nor become creator/contact impersonation. `cms.scenarios.registry` also has
  an unfiltered `user=None` projection: it is not a customer catalog read path.
- `ProvisionerLaunchIntent`/`OperationInput` preserve operation identity, not
  the complete initiating-principal/credential/executing-workload authority
  required here. `authorize_provisioner_payload` checks generation/domain
  ownership; its name does not prove current principal policy authorization.
- `CredentialSessionWebSocketBoundary` revalidates session state on incoming
  and outgoing messages. It has no idle timer and does not recheck resource
  policy. Range/NGFW consumers authorize at connect. A Guacamole bootstrap URL
  expiry or consumption does not establish termination of an existing tunnel.
  No common tested remote-revocation bound is established by these mechanisms.

## Authority and ownership decisions

Use `CredentialContext`, `PrincipalRef`, `ResourceScope`, `TargetRef`,
`CredentialCeiling`, `AuthorizationRequest` and the configured
`AuthorizationProvider`. Resolve active principals through `shared.principal_port`
and scope through `workspaces.services.resolve_resource_scope` /
`resource_scope_from_ids`; domains supply their own persisted resource references.
Keep shape validation in shared and ancestry/lifecycle resolution in workspaces.
No copied hierarchy, cross-layer foreign key, JSON authority bag or domain-local
scope validator. Missing scope never means installation or a default workspace.

Every baseline operation needs a registered action, exact target, trusted scope,
owning service check, credential admission and opaque failure mapping. Include
browser, API, direct service, retained admin/management-command, scheduled,
callback and stream paths. Extend the single catalog, model, relationship and
credential mappings together where coverage is absent. A scope-level content
action may authorize a resource resolved inside that exact scope; introduce a
new target type only for an identified resource-level policy need. Parent-scoped
creation authorizes the existing parent before allocating a resource identity.

Human and service administrators perform the same valid content/range operations
within the exact credential ceiling. Validate catalog/model administrator
coverage, including launch and access actions marked non-administrative today;
do not assume catalog administrative-set equality alone proves all operations.
Explicit denies, exact target ceilings, lifecycle, quota, cloud permissions and
CTF participation constraints still apply. Source ownership, creator attribution,
current caller, workload identity and application grant are separate facts.

CMS owns content and range intent/projections; Engine owns realized lifecycle,
execution, preparation, installation and remote capability state. Mission
Control uses CMS control-plane services and the existing allowlisted Engine
runtime facades (`scripts/check_layer_imports/layer_imports.yaml` at repo root).
CTF uses its CMS bridge and event-native participation/receipt checks. `config`
composes public service ports without importing models. Do not move policy into
controllers, cloud adapters, the provisioner or the plugin worker.

Customer-private assets require explicit account scope, with real subdivisions
only where applicable. Operator-shared catalog visibility is an explicit
publication/access fact; null organization, source kind, conformance, staff
status and a shared content digest do not publish a private asset. Sharing a
catalog entry does not share registry/source credentials, private artifacts,
preparation evidence or executable installation authority. Tenant administrators
retain their supported isolated adapter installation path; no new central
operator approval ceremony is implied.

Filter collections before count, pagination, search, export and serialization.
Re-resolve nested IDs and storage references inside that authorized scope before
reading bytes or secrets. Namespace caller-dependent caches, retry lookups,
object indexes and exports by authoritative customer/principal/target facts as
appropriate; a cache hit never replaces current authority. Neutral immutable
content may be deduplicated without sharing its access projection. Frontend
query state must be invalidated on account/principal changes.

Transfers authorize both persisted source and requested destination and preserve
the incumbent locked compare-and-set projection checks in
`cms.services._range_workspace_admin`, `_range_reassign` and the Engine rebind
facade. Keep owner change separate from scope change; preserve domain aggregate
guards (`shared.range_workspace_aggregate`), stable IDs and historical attribution.
Never repair conflicting CMS Request/RangeInstance/Engine Range projections by
guessing. Queued work, remote sessions and shared access derived from the old
binding must be invalidated; they cannot follow a resource into another account.

## Durable work and live connections

Extend the owning durable operation, using shared neutral shapes only where
several domains exchange the same fact. Persist the admitted action/target/scope,
initiating principal, non-secret credential identity and original exact ceiling,
and separately the executing workload identity. Retain immutable operation,
attempt/generation and request/audit correlation. A reference is not a grant:
dispatch/retry must resolve current principal and credential eligibility, current
scope/owner, live policy and the intersection with the original ceiling. A later
grant cannot widen queued intent. A mutable owner field cannot replace its actor.
Session credentials currently identify the principal UUID; that alone is not a
revocable per-session reference for durable work. Define its non-secret lifecycle
binding through the existing credential owner, never store a cookie or bearer.

Reuse `engine.launch_intents`, `engine.operation_inputs`,
`engine.retry_binding`, `cms.services._retry_safe_launch`,
`shared.operation_intent`, `shared.operation_envelope`, `shared.operation_results`,
`OperationResultInbox`, `RangeEventOutbox`, `engine.launch_interrupt` and the
cleanup-verification services. Preparation, plugin invocations and CTF scheduling
retain their own existing state machines, claim fences and attempt records;
`PreparationScopeLock.scope_digest` is a backend mutex, not customer scope.

Preserve atomic intent/input/reservation/audit persistence, PostgreSQL uniqueness
for concurrent first use, generation checks, equal-digest replay and conflicting
replay rejection. Scope/actor/ceiling additions affect the versioned intent
digest and retained-input compatibility, not merely dispatch argv. Reauthorize
operation lookup, replay and cancel without minting duplicate quota or warm-pool
effects. Do not make `on_commit` the only durable dispatch obligation. Keep
policy network calls outside long SQL critical sections; recheck persisted
generation/scope fences before the effect and preserve recovery for ambiguous
external outcomes rather than weakening the launcher's existing fencing.

Revocation blocks new user-authorized effects. Cancellation and required cleanup
use explicit, bounded system authority tied to the recorded resource/generation
and retained adapter/backend pins, with their own workload attribution. Missing
actor, deleted owner, an audit `SYSTEM` label or a failed authorization check
never creates that authority. Cleanup cannot launch, publish, rehome or grant
access. System purpose does not add a third `PrincipalRef` kind or a caller-set
bypass flag; keep the established service/workload identity explicit.
Record authentic results of already-started work and cleanup obligations
even after user revocation; do not discard evidence or resurrect access. A
callback must prove workload/source and exact operation/attempt/target identity;
queue possession, envelope parsing and payload actor fields are not that proof.

For sockets, compose session admission with the same resource action check before
connect/hydration, privileged input (including terminal input/resize), subscription
changes and protected output. Reuse `shared.channels.groups/payloads`, Channels
and the Redis transport. Revocation notification accelerates closure; bounded
periodic revalidation must cover idle sockets, missed notifications and workers
on different pods. The parked shared-notification feature cannot be a mandatory
security-delivery dependency. Never rely on process-local connection accounting
as a deployment-wide revocation registry.

Document a finite, enforced and tested bound per supported transport from committed
revocation to data-plane termination. Include policy propagation/recheck interval,
timeout and shutdown latency, plus dependency-outage behavior. Do not claim a
numeric bound before observing closure of the actual transport:

| Transport | Incumbent and required termination evidence |
| --- | --- |
| SSH/status/NGFW sockets | `mission_control.consumers`, `status_consumers`, `terminal_sessions`, `terminal_executor`, `shared.remote_access.TerminalConnectionFactory` and `engine.services._terminal`. Close SSH as well as WebSocket, cancel reader/recheck tasks, release capacity once, leave channel groups and suppress buffered output after denial. Test idle and output-only connections. |
| Guacamole SSH/RDP and uploads | `mission_control.guacamole_session`, `guacamole_bootstrap`, `GuacamoleClient` and CMS/Engine remote facades. Reauthorize queued bootstrap work and one-time consumption; revoke the active backend tunnel as well as browser access, including SFTP. Current `GuacamoleClient` creates URLs; an end-session extension belongs in this incumbent transport seam. Browser navigation, token TTL and bootstrap pruning are not termination evidence. |
| OpenVPN | `engine.services._vpn_sessions`, `cms.api.vpn_control` and repo-root `shifter/engine/openvpn/shifter_openvpn/controller.py`. Reuse the 15-second heartbeat/60-second lease and controller kill/firewall removal, adding current principal/policy checks to existing generation/owner/lifecycle checks. Verify healthy and failed-heartbeat bounds; those constants alone are not a measured bound for S6. Keep the controller's native workload authentication separate from the end user's application authority. |

Previously delivered passwords, private keys and presigned URLs cannot be recalled
by a portal policy check. Inventory direct access paths and enforce revocation at
their managed transport/guest credential/network boundary. Do not promise instant
object-URL revocation where only finite expiry exists; prevent subsequent
finalization/attachment and document the remaining bounded capability lifetime.

## Cross-cutting layers that the design must pass

| Layer | Canonical incumbent and required behavior |
| --- | --- |
| Authentication and policy | `config._drf_settings`, `config.service_identity`, `config.session_credentials`, `config.websocket_auth`, `shared.api.principals.authenticated_credential`, shared authorization port and `config.openfga_authorization`. Preserve bearer-first failure, session CSRF, exact issuer/audience, active principal, expiry/revocation, ceiling and fail-closed provider outcomes. ASGI retains `AllowedHostsOriginValidator` and temporary-CTF confinement. A service need not impersonate a human to use a valid operation. |
| HTTP/DTO parsing | Existing CMS/Mission Control serializers, `shared.api.closed_serializer.ClosedSerializer`, `shared.api.strict_json.ClosedJSONParser` and its `ModelSelectionJSONParser`, shared schemas and pinned RAES contracts under `shared.raes`. Reject unknown authority fields and malformed/duplicate/oversized input within the incumbent endpoint-specific limits; validate transport shape once, domain state at its owner. Do not duplicate the action/scope/credential DTO in API, worker or SDK. |
| Content, storage and guest input | `cms.services._content_ingestion`, `_tenant_pack_upload`, `cms.scenarios.registry/pack_validation`, `shared.schemas.raes_package_source`, `shared.raes.object_source`, `cms.assets`, `_uploads_finalize`, `shared.uploads.inspection`, `shared.sftp_root`, `shared.remote_access` and `shared.receipt_validation`. Preserve provenance-only records, path/archive containment, immutable digests, size/magic-byte inspection, signed upload purpose/expiry and conditional staging-to-install copy. Reauthorize upload completion/cancel/download and secret reveal against current scope; extend user-only bindings for principals. Receipt signatures still need exact event/participant/objective/range-generation binding and authoritative completion checks. |
| Sources, sharing and device operations | `engine.services._model_sources`, `_sharing_authority`, `_sharing_resolution`, `_preparation_operations`, `_runtime_plugins`, CMS `_model_access_sharing`, `_credentials`, `_agents`, `_ngfws` and their public facades. Reuse typed source selections/revisions, authority invalidation, admission, egress/lease/quota and device lifecycle checks. Preparation cloud grants, runtime plugin installations, application credentials and guest/source credentials are distinct contracts; none substitutes for customer authority or grants cloud IAM. Carry scope through derived artifacts, source observations and sharing caches as well as their parent records. |
| Queues, scheduler and callbacks | `shared.messages.envelope/payloads`, `shared.management.commands.run_worker`, Engine/CMS/Mission Control handlers, `CTFScheduledTask` and `ctf.services.communication`. Preserve closed event contracts, queue delivery/retry semantics, claim fences and domain idempotency. Transport/workload authentication and exact operation binding precede effect application; parsed message fields cannot grant authority. Extend existing security invalidation delivery independently of best-effort UI status fanout. |
| Persistence and external effects | Owning Django models, forward migrations, named constraints, `transaction.atomic`/row locks, public operation bindings, outbox/inbox and scheduler claims. Extend existing scalar scope projections consistently; check bulk/query paths that bypass model validation. Pin credential/source/adapter revisions and operation versions; failures never silently rehome resources or redispatch with new authority. |
| Secret handling | `cms.credential_encryption`, `shared.field_encryption.EncryptedStringField`, `engine.secrets`, `shared.cloud.owned_secrets`, `shared.credential_delivery` and restricted projections. Preserve encrypted storage and provider-owned secret references; authorize before resolution. No raw browser/API token in a job, log, audit row, result, replay digest or adapter input. Registry pull credentials stay in the existing encrypted/pull-secret lifecycle. Neither secret existence nor decryption grants access. |
| Worker/SDK validators | `shared.runtime_plugin_binding`, `shifter_adapter_sdk.runtime` closed manifests/input/result parsing, `shared.cloud.runtime_plugins`, preparation contracts and Engine controllers. Preserve executable digest, exact invocation/operation, admitted target/channel/capability and output bounds. A pack cannot authorize executable installation. Plugin workers remain isolated, without portal/cloud credentials or imported application internals; the host resolves typed actions. Compatibility success is not live-range qualification. |
| Environment/configuration | `config/env-manifest.json`, `config._env_manifest`, `config._runtime_env`, typed runtime settings, repo-root `shifter/installation/{schema,loader,render,runtime_inventory}.py`, `engine.ecs._env` and `shared.cloud.sensitive_env.split_env`. Any new timeout/binding passes all applicable inventories and positive finite range checks. No ad hoc environment flag or new secret is required by this guidance. Secret-bearing values use the existing mounted-file/Secret path; a `_REF` or `_URL` name must actually contain a non-secret reference. |
| OS and deployment | `engine.launch_intents.validate_provisioner_command`/`command_from_payload`, the standalone provisioner CLI/input reader, Kubernetes task manifest/secret builders, repo-root `platform/k8s/gcp/base/validatingadmissionpolicy-provisioner-jobs.yaml`, chart `templates/{rbac-job-launcher,runtime-plugins}.yaml`, NetworkPolicy and Terraform IAM bindings. Keep argv to canonical identifiers; authority travels through persisted operation references. Never put bearer material in argv, literal Pod env, labels, guest metadata or shell traces. `SHIFTER_PLUGIN_INPUT` is already classified sensitive and must keep Secret-backed transport. Do not relax command/env admission, worker RBAC or network containment to pass a new field. |
| Errors, audit and observability | `shared.exceptions`, `cms.exceptions`, `shared.errors`, `shared.api.errors`, `shared.audit`/`audit_adapter`, `RequestAudit`, `principal_actor_fields`, `shared.log_sanitize`, existing request IDs and `WebSocketCloseCode`. Use bounded authored codes/messages and opaque inaccessible-resource outcomes; do not reflect validation, SDK, subprocess or provider text. Sanitize stream `error_message` and persisted failure fields too. Keep initiating principal and executing workload separately attributable, preserve strict security-mutation audit and v2 integrity without rewriting v1 history. Sanitizing log newlines does not redact customer/secret content. |
| Frontend and public contracts | `shared.api.schema`, `openapi/v1.json`, generated `frontend/src/api/schema.d.ts`, `frontend/src/api/client.ts`, principal context, TanStack Query and Mission Control routes. Capabilities are presentation hints; server checks remain authoritative. Preserve bounded downloads, session/CSRF, no-store credential responses and existing one-time Guacamole handoff. Do not add bearer storage, token query authentication or customer-private URLs to browser persistence/telemetry. |

## Extension seams and evidence

The next content type or account placement should vary by registered action,
exact target and resolved `ResourceScope` through an owning service. Extend
account/organization range ancestry and bounded domain inventories in the same
canonical policy contracts; do not force a workspace or create a parallel
individual-account workflow. Another transport should provide revalidation and
termination through its existing connection/client/controller seam, with an
injected clock and bounded timeout configuration. Another operation family
should consume the shared authority facts in its own existing durable workflow.
None requires a general job bus, token protocol or capability framework.

Required evidence reuses `tests/shared/authorization`, the released
OpenFGA/PostgreSQL harness, `tests/cms/test_account_scope_schema.py`,
`test_retry_safe_launch.py`, upload/plugin/preparation tests,
`tests/cms/services/test_range_access_authorization.py`, Engine launch/outbox/
operation-apply/interrupt/cleanup and PostgreSQL concurrency tests,
`tests/config/test_session_credentials.py`, Mission Control consumer/bootstrap
tests, VPN service/controller tests and receipt-binding tests. Cover valid human
and service administrators, least-privilege ceilings, individual-account ranges,
sibling accounts, stale/rebound/archived resources, credential/owner/policy
revocation, incomplete provider results/outages, lost response/replay, competing
cancel/dispatch, stale callbacks, and actual remote teardown with lost notifications.
Secret-marker tests must inspect durable payloads, emitted errors/logs, generated
Job specs and argv; endpoint-only mocks cannot establish those boundaries.

Repository checks remain `.ground-control.yaml`, `.gc/plan-rules.md`,
`docs/adr/index.yaml`, `docs/adr/exceptions.yaml`, `.importlinter`,
`scripts/check_layer_imports`, `check_model_fks`, `.gitleaks.toml`, pre-commit and
`.github/workflows/_quality.yml` (paths here are repo-relative). Architecture work
runs `python3 scripts/adr_guard/adr_guard.py --all --level ci`; subsequent source
work runs its native lint/type/OpenAPI and PostgreSQL/OpenFGA checks. Changed
deployment artifacts also pass actionlint, TFLint, kube-linter/kubeconform and
chart/admission tests as applicable. No guard weakening or undocumented exception.

## Non-goals and anti-patterns

This preflight implements no runtime behavior and does not authorize S8 or source
work past #2308's gate. It does not redesign CTF participation, cloud IAM, RAES,
model-access policy, credential issuance, the queue framework or storage backend.
No new policy evaluator, authorization app, exception hierarchy, duplicate DTO,
token issuer, custom cryptography, approval workflow or tenant-specific fork.
Application account isolation does not claim separate cloud/database/key/network
boundaries. Keep private pack content, identities and acceptance evidence in their
own repositories; use synthetic fixtures and the public adapter SDK here.

Avoid HTTP-only checks, staff/superuser fallbacks after policy denial, fake service
users, global/unfiltered catalog caches, inference of authority from payloads,
`actor=None` cleanup escalation, scope digests mistaken for tenants, owner IDs
mistaken for initiating actors, token TTL mistaken for live termination, and audit
records mistaken for workflow state. Preserve the single coordinated S8 cutover;
old authority is neither a fallback nor an extra grant/veto after activation.
