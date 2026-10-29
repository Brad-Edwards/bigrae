# Content, range, background and streaming authority: S6 proposal

Status: approved by the user on 2026-10-13 for the exact-design authorization required by
[issue #2308](https://github.com/Brad-Edwards/bigrae/issues/2308).
The approval authorizes this C4 source integration. Production activation remains S8.

Scope: [issue #2319](https://github.com/Brad-Edwards/bigrae/issues/2319), custom
integration C4. The binding architecture is ADR-066 and the
[S6 preflight](../architecture/content-range-authority-preflight-2319.md).
Dependencies #2314, #2315 and #2316 are merged. S8 remains the single production
authority cutover.

## Specific gaps and proposed integration

| Observed gap | Proposed change through the existing owner |
| --- | --- |
| CMS assets and preparation callers assume human ownership; the catalog lacks complete content actions. | Add scalar principal and explicit customer-scope facts to the owning CMS/Engine records. Register content/source/credential/preparation actions at their existing installation/account/organization/workspace parent. Resolve the resource in that parent before policy. Extend the shared catalog, generated OpenFGA model, credential mapping and administrative coverage together. No new policy resource type is proposed. |
| Range policy ancestry and inventory assume workspace/event placement despite existing account fields. | Add parent-scoped launch for account and organization placement and extend Engine's SQL inventory and the existing provider ancestry composition. Check the actual range/event association and all CMS/Engine scope projections. An individual range has no fabricated workspace. |
| Service credentials cannot traverse human-only CMS/Mission Control operations. | Move the principal-neutral authority check to the owning service using the existing CredentialContext and shared provider. Preserve lifecycle, quota, cloud authority and CTF participation checks. Keep legacy authority solely until the coordinated S8 activation; it cannot rescue or veto the activated policy decision. Retain historical human attribution separately from current principal ownership. |
| Existing launch/preparation/plugin records lack complete initiating and executing authority. | Extend those records and versioned immutable inputs with the admitted action/target/scope, principal, non-secret credential identity, original exact ceiling and separate workload identity. Reuse the current operation, attempt, claim, generation, inbox/outbox and replay digest. Reauthorize at lookup, dispatch, retry and cancel; preserve evidence for effects already started. |
| A browser CredentialContext uses its principal UUID as its credential identity. | Extend Django's established database-session backend/model with an indexed non-secret credential UUID and scalar principal reference. The management-owned resolver looks up and validates the live Django session through this UUID. Session cycling creates a new credential reference; logout, expiry and principal/provider revocation invalidate the old reference. Jobs and transport messages carry only the UUID, never the session cookie/key or bearer. This is session metadata integration using Django primitives, not a new credential protocol. Activate the backend and map or revoke old sessions only at S8. |
| Socket admission has no idle revalidation and consumers only check resource ownership at connect. | Extend the incumbent Channels boundary and owning remote services with connect, privileged-message, protected-output and bounded periodic policy revalidation. Native channel notification accelerates closure; periodic checks cover notification loss and idle/output-only clients. On denial or outage, close the transport, cancel its reader/recheck tasks, release capacity once and discard subscriptions. |
| Guacamole URL expiry does not terminate an existing SSH/RDP/SFTP tunnel. | Persist the non-secret active connection identity and its originating authority in the existing Guacamole session workflow. Extend GuacamoleClient through the installed server's supported active-connection termination interface. Revalidate queued bootstrap work and one-time handoff; terminate the backend connection as well as browser access on revocation. |
| OpenVPN rechecks generation, owner and lifecycle but lacks current principal/policy checks. | Extend the existing VPN service heartbeat with credential/principal and policy revalidation. Keep its current 15-second heartbeat, 60-second lease, native controller kill and firewall removal. Controller workload admission remains separate from user application authority. |

The session backend extension and Guacamole termination integration are explicit
C4 source choices covered by this approval. Implementation must verify
the incumbent session settings and installed Guacamole API before changing either;
an incompatible backend/API requires a documented design revision, not a fallback
credential store or simulated teardown.

The session extension follows Django's documented
[database-backed session extension points](https://docs.djangoproject.com/en/6.0/topics/http/sessions/#extending-database-backed-session-engines).
Guacamole's [JSON authentication contract](https://guacamole.apache.org/doc/gug/json-auth.html)
defines when an admission payload expires; it does not establish active-tunnel
termination for this integration. The upstream
[active-connection REST resource](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/activeconnection/ActiveConnectionResource.java)
is an integration seam to qualify against the deployed authentication provider,
not evidence that Shifter currently has a working revocation path. Qualification
selected the provider-independent native session invalidation endpoint instead:
[`DELETE /api/session`](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/session/SessionResource.java).
The pinned 1.6.0 implementation invalidates the session and closes its associated
tunnels, including those authenticated through JSON. Native synthetic SSH/SFTP
qualification and shutdown limits are recorded in the
[technical guide](../technical/shifter_platform/content-range-authority.md).

## Exact authority sequence

1. Admission resolves one active human/service principal and the server-derived
   credential ceiling. Unsafe browser requests retain Django CSRF. Provider
   verification, cloud IAM and application policy remain separate boundaries.
2. The owning service resolves the resource or creation parent from SQL and
   validates account, organization and optional workspace ancestry through
   workspaces.services. Caller-supplied scope cannot rehome a persisted resource.
3. The shared authorization provider checks the exact registered action, target,
   resolved scope and credential ceiling. Missing data, deny, incomplete decisions,
   timeout or evaluator failure prevent the effect. Collections are filtered
   before counts, pagination, exports, cache projection or storage URL signing.
4. Before committing a durable request, record its immutable initiating identity,
   original ceiling, scope and generation in the existing operation transaction
   with audit/reservation/input state. Pin referenced sources/adapters and preserve
   the existing versioned intent digest and equal/conflicting replay rules.
5. At dispatch/retry, re-resolve credential eligibility and scope and intersect
   current authority with the original ceiling. A later grant cannot widen intent.
   Keep network policy evaluation outside long SQL critical sections and verify
   persisted generation/scope fences before effects. Retain existing uncertain
   external-outcome recovery.
6. Required cancellation/cleanup uses explicit operation-family system authority
   restricted to the recorded resource/generation/backend/adapter. It cannot
   launch, publish, transfer or grant access. Record the executing workload
   separately; a missing/revoked initiating actor never becomes system authority.
7. Transfers check persisted source and requested destination independently,
   preserve stable IDs and historical attribution, and invalidate queued work and
   remote access bound to the previous scope. Conflicting projections fail closed.
8. Continuing access repeats the same resource decision and invokes the actual
   transport termination seam. Neither a successful URL issue nor browser closure
   is evidence that the backend connection ended.

## Isolation, secrets and external adapters

Operator-shared publication is explicit. It exposes only the allowed catalog
projection, not customer assets, source credentials, preparation evidence or
executable installation authority. Cache/export/storage keys include authoritative
scope and actor/target dimensions as needed; a cache hit still rechecks authority.
Previously issued object URLs have a finite capability lifetime: block new
issuance and finalization after revocation and document the residual lifetime.
Do not claim that portal revocation recalls a previously delivered key or URL.

No application token, session cookie/key, provider credential, private pack content
or raw provider exception enters a job payload, command argument, cloud label,
audit record, public repository record or client cache. Retain encrypted
credentials and the established secret-reference lifecycle. Keep execution in the
isolated plugin worker using the public SDK and typed host actions. Tenant
administrator installation remains supported; pack content does not grant
executable-installation authority.

## Verification and delivery

The source implementation must deliver the following before reporting readiness:

- Map all 67 currently published CMS/Mission Control operations and the retained
  non-API jobs, callbacks, streams and admin paths. Coverage checks detect additions
  requiring classification. Test owning-service enforcement independently of HTTP.
- Test full human/service administration, exact ceilings, account-level ranges,
  sibling-account denial and stale/archived/rebound resource rejection. Test
  query/count/cache/export/download filtering and authorized transfer boundaries.
- Test durable replay, competing cancel/dispatch, session/token/service/policy
  revocation, explicit cleanup, separate workload attribution, conflicting replay
  and stale callback rejection using the incumbent PostgreSQL/OpenFGA harness.
- Measure transport closure for idle and output-only sockets, SSH reader cleanup,
  Guacamole SSH/RDP/SFTP teardown and healthy/failed OpenVPN heartbeats. Inject
  notification loss and evaluator outage. Specify each supported finite bound
  from committed revocation through recheck/timeout to observed data-plane closure;
  do not advertise an unmeasured numerical guarantee.
- Inspect persisted inputs, outputs, logs, Job specs and argv for secret markers.
  Use synthetic fixtures and preserve existing SDK/worker/provisioner validators.
- Update user/technical documentation, generated API/client contracts where
  changed and requirement traceability where applicable. Run native lint/import
  checks, ADR guard, mandatory review, pre-commit, hosted CI and Sonar.

Approval is limited to this existing-service C4 integration. It does not authorize
a new job framework, token/capability protocol, policy evaluator, external service,
hosting destination, private-content transfer, production deployment or PR merge.
