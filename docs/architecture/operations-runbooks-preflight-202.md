# Operations runbooks architecture preflight (#202)

> **Historical implementation record.** This issue-scoped preflight is not
> current-system authority. See the
> [current-state handbook](../technical/architecture.md), the implementation,
> and accepted ADRs.

Status: pre-implementation guidance. No runbook is implemented here. The
supplied issue defines the runbook catalog and GEN-001 supplies the
documentation-deliverable requirement. ADR-022 and
`docs/adr/documentation-coverage.yaml` remain the repository-wide coverage
contract; this documentation issue does not by itself create a second feature
registry or a new runtime concept.
Repository behavior in this note was inspected at revision
`a80fbc061ad552ac027b1ed863e84373e3470fe8`.

## Traceability scope resolution

Issue `Brad-Edwards/shifter#202` began as the 22-runbook operations handoff. Its
required repository-wide verification then exposed expired ADR-004 and ADR-019
exceptions and unowned production quality surfaces. The repository owner
explicitly expanded this delivery to fix those failures on the same pull request
instead of renewing exceptions or walking past failing checks. The issue's
Requirements section now names both requirements, and its revised implementation
plan records the combined scope before publication.

Trace the runbook catalog and its safety contract to GEN-001. Trace the blocking
quality ownership, exception removal, boundary-port repair, and reduced mock
baseline to GEN-002. This is one authoritatively expanded delivery with two
separately mapped concerns, not two issue identities. No time-bounded exception
is introduced or extended.

## Decision boundary

Create the 22 issue-named runbooks as 22 separate files under
`docs/runbooks/`, plus a small `docs/runbooks/index.md` that states shared
safety conventions and links every runbook. Keep the `RB001` through `RB022`
identifiers stable for handoff and incident references. Each runbook must use
the issue's `Symptom`, `Investigation`, `Resolution`, and `Prevention`
sections, and may add applicability, prerequisites, escalation, and recovery
verification.

The issue's service nouns describe an older AWS architecture; they are not
permission to document nonexistent Step Functions or provisioning/cleanup
Lambdas. The current control plane is Helm on GKE or EKS, provisioning runs as
Kubernetes Jobs, and lifecycle delivery is backed by durable database records
and Pub/Sub or SNS/SQS. Preserve each incident's intent while naming the
actual provider-specific component. Mark a provider `Not applicable` when the
repository has no corresponding deployed capability. Do not imply that source
configuration proves a particular live tenant is enabled, healthy, or
qualified.

The runbook prose documents existing behavior rather than creating a new
runtime architecture decision. The accepted catalog contract test reuses the
existing ADR-guard unittest suite and extends the ADR-022 enforcement evidence;
it does not add another runtime checker, manifest, or workflow. The expanded
quality-closure work changes accepted governance enforcement, so it updates the
ADR-004/ADR-019 evidence and technical enforcement documentation in the same
delivery. Several requested recovery operations have
no qualified incumbent at the inspected revision: provider-safe orphan
mutation, a production target in the top-level deploy dispatcher, an approved
one-shot reconciler transport, GCP Artifact Registry cleanup, and GCP log
export to XDR. The affected runbook must state the capability gap and
escalation boundary. Adding or qualifying missing automation is separate
architecture work, not something to synthesize in prose.

## Required applicability and current owners

| Runbook | Current repository surface and implementation guardrail |
| --- | --- |
| `RB001-RangeProvisioningFailed` | Correlate the CMS request ID, Engine operation/generation, `ProvisionerLaunchIntent`, launcher worker, provisioner Job, result inbox/applier, and provider resources. Step Functions is legacy metadata, not the current workflow. Retry only through the existing retry/lifecycle service so the same operation identity is recovered. |
| `RB002-RangeTeardownStuck` | Use the persisted backend/ownership binding, destroy operation, result application, and cleanup-outcome projection. A logical `DESTROYED` status is not proof that provider resources are absent. There is no cleanup Lambda to invoke. |
| `RB003-OrphanedRangeResources` | Use independent provider inventory plus the Engine-owned operation, ownership, cleanup-outcome, and `RangeCleanupVerification` evidence. The AWS-only `shifter-ops reconcile_ranges` dry run may contribute bounded legacy EC2 evidence, but its execute path terminates instances and performs non-transactional direct SQL outside the lifecycle/result/cleanup-verification boundary; it is not a qualified production recovery surface. Neither provider has a canonical mutating orphan command at this revision. Preserve evidence and escalate rather than invoking that execute path, translating it to GCP, or deleting resources by name. |
| `RB004-PortalDown` | Start at public `/health/`, ingress/backend health, `portal-web`, dependency readiness, rollout state, and certificate/DNS. GCP uses GKE/GCE ingress and ManagedCertificate; AWS uses EKS/ALB. `/health/` is a coarse public readiness endpoint, not a diagnostics API. |
| `RB005-DatabaseConnectionErrors` | Branch between Cloud SQL and RDS, and between migration, runtime, Guacamole, OpenFGA, and provisioner duties. Reuse Cloud SQL Auth Proxy/native IAM or RDS IAM/password boundaries as deployed; never flatten them into one shared credential. |
| `RB006-LambdaErrors` | Provisioning failures belong to workers and Kubernetes provisioner Jobs, not Lambda. Only document a function branch that actually exists: AWS Cognito pre-sign-up/client-secret rotation or Redis rotation, or the optional GCP Identity Platform before-create function. Preserve each function's distinct invocation, IAM, timeout, retry, and logging owner. Do not use this file as a duplicate of RB001. |
| `RB007-LogsNotFlowing` | AWS has the Terraform-owned CloudWatch subscription -> Firehose -> encrypted S3 -> SQS notification path, but only for the configured `source_log_group_names`; it is not an all-workload Kubernetes log pipeline. A separately generated CloudTrail/XDR connector stack is a different owner and must not be diagnosed or replayed as this pipeline. GCP range audit telemetry goes to Cloud Logging and no repository-owned XDR export sink exists. Arrival in S3/GCS is not proof of ingestion by XDR/XSIAM. |
| `RB008-SQSDLQMessages` | The log-notification SQS DLQ is AWS-only. GCP's Pub/Sub dead-letter subscription carries platform lifecycle events, not log-object notifications; diagnose it through the worker/event contract instead of treating it as an SQS/XDR queue. Never replay before classifying poison versus transient messages and confirming idempotency. |
| `RB009-UserLockedOut` | Distinguish Identity Platform, Cognito/OIDC, local accounts, and temporary CTF accounts. Provider-bound password/MFA recovery stays at the provider; `management.lifecycle` owns application activation/suspension/deactivation, and the administrator reset service applies only to eligible local accounts. |
| `RB010-AgentUploadFailed` | Reuse CMS upload admission/finalization, provider-neutral object storage, signed upload tokens, immutable installed-object identity, server-side size/header checks, scoped custody, audit, and cleanup reconciliation. `cms.assets.s3` names are compatibility internals and do not make GCP uploads S3 operations. |
| `RB011-S3StorageQuotaExceeded` | The enforced limits are application agent-upload limits (`AGENT_MAX_FILE_SIZE_MB` and `AGENT_USER_STORAGE_QUOTA_MB`), including pending scoped reservations; this is not cloud bucket capacity. Retire agents through the owning service and cleanup path, not by deleting objects or editing byte counts. |
| `RB012-PortalDeployment` | Use only a target exposed by the manual, environment-selected `deploy.yml` workflow and its reusable provider workflow. Preserve protected environments, WIF/OIDC, quality and exact-digest gates, dedicated migration, Helm/rendering, rollout/readback, and post-deploy smoke. A branch push does not deploy, and the inspected dispatcher exposes development/named-tenant targets but no production target. The runbook must record that production deployment is unqualified and escalate; it must not manufacture a path by directly invoking a reusable workflow or copying its steps. |
| `RB013-InfrastructureDeployment` | Terraform remains the infrastructure owner; use saved plans, remote state locks, reviewed environment inputs, and a canonical workflow/bootstrap route only where one is actually exposed. Do not turn `terraform apply`, `kubectl apply`, local Helm, or a reusable child workflow into a parallel production deployment path. The absent top-level production dispatch is a capability/approval gap, not permission for a hand-run apply. |
| `RB014-RDSBackupRestore` | Make provider applicability explicit: RDS on AWS, Cloud SQL on GCP. Restore affects durable application state and may also affect Guacamole/OpenFGA databases on the shared instance. Freeze conflicting writers, preserve the source backup, validate an isolated restore where supported, reconcile migrations/identities/events, and prove application plus cleanup consistency before cutover. The GCP module enables automated backups but does not establish a repository-qualified PITR procedure. |
| `RB015-DatabaseMigrations` | Both Kubernetes providers use a dedicated migrator identity and one migration Job; long-running pods carry `SKIP_MIGRATIONS=1`. Reuse the deploy renderers (`render_migration_job.py` on GCP and `scripts.bootstrap.aws_eks` on AWS). Never run migrations concurrently from web/worker replicas or with runtime DB credentials. |
| `RB016-RDSMaintenanceWindow` | AWS owns a configured RDS backup/maintenance window and the pending-modifications checker; prod intentionally permits deferred changes while dev fails on pending changes. The GCP Cloud SQL module does not declare an equivalent maintenance-window contract, so a GCP runbook must report provider events and the gap without claiming Terraform controls the window. |
| `RB017-StaleRangeCleanup` | The `worker-reconciler` runs `reconcile_range_events --loop`; it converges CMS projections and calls the canonical bounded lease-expiry service. Inspect or restart that deployed worker through the established workload route. Although the command supports one-shot execution, `mcp/ops` does not allowlist it and the repository exposes no approved provider-neutral one-shot operator transport at this revision; document escalation instead of inventing `kubectl exec` or arbitrary SSM. There is no stale-range Lambda, and this reconciler is not a general orphan-resource deleter. |
| `RB018-ECRStorageCleanup` | AWS ECR repositories already have a Terraform lifecycle policy retaining the last 30 images. GCP Artifact Registry currently has no cleanup policy. Never delete a digest referenced by running workloads, rollback, release evidence, provisioner inputs, or guest-image promotion; a GCP automated policy requires separate Terraform/design work. |
| `RB019-CheckSystemHealth` | Compose existing signals: public readiness, ingress/backend health, rollout/pod status, worker heartbeat probes, DB/Redis, Pub/Sub or SNS/SQS backlog, certificate/DNS, exact running image IDs, and the existing post-deploy smoke. Keep readiness, process liveness, queue health, and live range qualification distinct. |
| `RB020-InvestigateLambdaTimeout` | For an actual AWS Lambda or GCP function, first identify its Terraform owner, configured timeout, invocation mode, retry owner, and downstream dependency; the identity hooks and rotation functions do not share one timeout/recovery policy. For range operations, inspect provisioner Job deadline/runtime, launcher state, provider-operation waits, and operation/result evidence instead. Do not label every timeout a Lambda problem or blindly raise timeouts; classify capacity, provider throttling, dependency latency, guest readiness, and stuck result application first. |
| `RB021-InvestigateHighRDSConnections` | Branch between RDS and Cloud SQL and inventory web, worker, migrator, Guacamole, OpenFGA, provisioner, and proxy connections. Django currently uses `CONN_MAX_AGE=0`; replica/worker counts and separate duties matter. Do not first raise `max_connections`, share credentials, or restart the database. |
| `RB022-FirehoseDeliveryLag` | AWS-only: use the Terraform-defined `DeliveryToS3.DataFreshness` alarm, Firehose error log, S3 error prefix, KMS/IAM, source subscriptions, and object arrival. Keep it distinct from the downstream SQS consumer and from GCP Cloud Logging. The current threshold is 900 seconds with a five-minute Firehose buffer. |

## Canonical incumbents to reuse

| Concern | Canonical incumbent | Runbook boundary |
| --- | --- | --- |
| Current architecture and ownership | `docs/technical/architecture.md` | Treat PostgreSQL, operation records, object storage, cloud secret stores, queues, and Terraform state as distinct authorities. Do not use an observability projection as lifecycle truth. |
| Deployment selection/config | `shifter/installation/**`, `shifter.yaml`, `scripts/gcp/render_runtime_env.py`, `scripts/bootstrap/deploy.py`, `scripts/bootstrap/aws_eks.py` | Provider/environment selection and runtime shapes already exist. Do not add a runbook-specific config file, env parser, backend selector, or copied resource inventory. |
| Deployment workflow | `.github/workflows/deploy.yml`, `_gcp-dev.yml`, `_shifter-platform.yml`, `_shifter-engine.yml` | Link to and invoke a protected top-level target that actually exists; do not reproduce its secret hydration, migration, apply order, or release gates as hand-run shell steps. Reusable child support for `prod` does not make a production entry point: the inspected dispatcher has none. |
| Infrastructure inventory | `platform/terraform/gcp/modules/platform-core`, AWS environment roots and `platform/terraform/modules/**`, Terraform outputs/state | Resolve actual names from the selected environment and outputs/labels. Examples use placeholders and never encode tenant project IDs, domains, accounts, or private topology. |
| Kubernetes workloads | `platform/charts/shifter`, `platform/k8s/gcp`, chart values schema and provider renderers | Use actual Deployment, Job, Service, BackendConfig, probe, namespace, service-account, and NetworkPolicy names. Do not invent ECS/Lambda equivalents for Kubernetes workloads. |
| Range lifecycle and persistence | `cms.services`, `engine.services`, `shared.enums.ResourceStatus`, Engine operation input/result records, `ProvisionerLaunchIntent`, operation-result applier, range-event outbox/reconciler | Recovery goes through the owning services and generation fences. No direct status SQL, soft-delete SQL, hand-built event, copied status enum, or second retry workflow. |
| Existing operator commands | `reconcile_range_events`, `drain_provisioner_launch_outbox`, `apply_operation_results`, `drain_range_event_outbox`, `prune_agent_uploads`, `run_post_deploy_smoke`; `mcp/ops` only for a separately qualified declared AWS surface | Use a named command only through an approved transport, with its bounds, locks, idempotency, and policy gates. Do not infer transport authorization from command existence. `reconcile_ranges execute` is specifically excluded: dry-run/two-phase policy does not repair its direct, non-atomic cross-domain mutation or produce cleanup-verification evidence. |
| Health and verification | `/health/`, chart probes/BackendConfig, `scripts/smoke-test.sh`, `cms.post_deploy_smoke`, `scripts/gcp/verify_running_image_ids.py`, existing deploy readbacks | State exactly what each signal proves. A green process, HTTP probe, Terraform apply, or smoke is not proof of every dependency or absent residual resources. |
| Identity/account recovery | Identity Platform/Cognito integration, `management.lifecycle`, `management.password_reset`, Administer APIs, `shared.audit` | Preserve provider/local/CTF origin and current authorization. Do not set passwords for provider accounts, toggle `auth_user` directly, or bypass MFA/allow-list checks. |
| Upload/quota | Public `cms.services` facade, its owning upload internals, `cms.assets.validation`, `shared.cloud.ObjectStorage`, `AgentUpload`/`AgentUploadBudget` | Operators and cross-layer callers use the public service facade. Keep validation, quota reservation, immutable finalization, cleanup custody, and audit together. No imports from private split modules, direct bucket cleanup, or duplicate quota formula. |
| AWS log aggregation | `platform/terraform/modules/log-aggregation` and each environment's `source_log_group_names` | Reuse the exact enabled Firehose/S3/SQS/KMS/alarm topology. Do not infer coverage for unlisted log groups or claim its downstream XDR consumer is implemented here. Keep the separately generated CloudTrail/XDR connector stack distinct, and never copy generated connector values or URLs into a runbook. |
| GCP event/logging | `platform/terraform/gcp/modules/portal/messaging`, `docs/ops/gcp-range-audit-telemetry.md` | Pub/Sub lifecycle DLQ and Cloud Logging telemetry are separate systems. Do not create a fictional GCP Firehose or conflate event messages with logs. |
| Logging, errors, and audit | `shared.log_sanitize`, `shared.api.errors`, `shared.errors`, `shared.audit`, provisioner `log_redact.py`, `mcp/ops/audit.js` | Correlate by request/operation ID and keep public output coarse. Reuse exception/error envelopes; do not add a runbook exception taxonomy or paste raw provider/SQL output into tickets. The MCP JSONL audit is local and best-effort, so it is diagnostic context, not durable compliance evidence. |
| Privileged operator policy | `.shifter.yaml`, `mcp/ops/policy.js`, `mcp/ops/SECURITY.md` | A qualified AWS MCP mutation retains environment confirmation, dry-run/two-phase execution, rate caps, idempotency, audit, and apex approval. These controls do not qualify semantically unsafe mutation logic. Raw MCP error text is operator-private and is not safe for public incident records. |
| Existing operations docs | `docs/ops/**`, `docs/dev/**`, `docs/qa/range-functional-smoke.md`, `docs/index.md` | Link to canonical detailed procedures rather than copying them. Update the canonical doc when behavior changes; do not let 22 summaries drift into 22 alternate authorities. |

## Shared runbook contract

Every file should begin with its stable ID, incident intent, applicable
provider/capability, required operator role, operational owner/escalation route,
and the source revision or date against which commands were verified. Record
source verification separately from the last successful rehearsal; configured
behavior is not live proof. There is no path-specific `docs/runbooks/**`
CODEOWNERS rule at the inspected revision. If review ownership is required,
adding it is a separately reviewed guardrail change rather than an incidental
documentation edit. Use the same parameter seam throughout:

- provider and deployment environment;
- cloud account/project, region/location, cluster, and namespace resolved from
  the selected deployment's canonical outputs;
- request ID and operation ID for range work;
- a bounded time window for logs and metrics;
- explicit dry-run/execute or observe/mutate mode where the incumbent supports
  it.

The next tenant, region, or provider must be supportable by changing those
inputs and the applicability branch, not by copying a runbook. Do not hide
provider differences behind invented generic resource names: the shared
incident intent is portable, while commands and proof criteria remain
provider-specific.

Investigation steps are read-only and precede mutation. Resolution steps name
the expected precondition, exact scope, existing safe mutation surface,
bounded wait/retry, success readback, rollback/escalation condition, and
evidence to retain. Prevention links to the Terraform, workflow, service, or
monitoring owner; a runbook must not prescribe one-off console drift as a
permanent fix.

## Cross-cutting layers the design must pass

- **Documentation publication:** `docs/runbooks/**` is part of the public
  ADR-038 MkDocs site; it has no operator-authentication boundary. The prose and
  examples therefore contain only synthetic placeholders and public-safe
  architecture. Reading a command never grants authority to run it.
- **Authentication and authorization:** cloud inspection uses the operator's
  purpose-scoped WIF/OIDC or established local operator identity. Deployment
  mutations use protected GitHub Environments. Application account/range
  changes use the authenticated Administer/CMS/Engine service boundary. A
  Kubernetes service account, cloud task identity, database login, or staff bit
  does not substitute for user/resource authority. Production is never an
  implicit default.
- **Secret handling:** commands and examples carry non-secret resource
  references only. Never instruct operators to print a Kubernetes Secret,
  access a secret payload to stdout, dump process environments, show all
  Terraform JSON outputs, expose a DB DSN, or paste signed upload/Guacamole URLs,
  tokens, passwords, private keys, cookies, MFA seeds, or credential-bearing
  provider errors. Secret values stay in provider secret stores, stdin, or the
  existing short-lived `0600` temp-file paths with cleanup.
- **Environment/config shapes:** `shifter.yaml` is validated by
  `shifter/installation`; GCP runtime values flow from Terraform outputs through
  the renderer and Helm `values.schema.json`; application environment reads are
  inventoried by `config/env-manifest.json`; AWS inputs stay in the established
  environment roots and protected workflow payloads. A runbook reads those
  projections but does not create another YAML schema, `.env` contract, name
  map, or secret inventory. Resolve only narrow, named, non-secret Terraform
  outputs; never dump all JSON outputs to discover names.
- **Input validation and policy gates:** prefer typed/named management commands,
  closed workflow inputs, Terraform validation, Helm schema validation, and
  `mcp/ops` Zod plus capability policy. Do not replace them with free-form SQL,
  `eval`, shell command strings, arbitrary SSM, or console edits. Validate
  environment, provider, identifier shape, ownership labels, and current state
  immediately before a destructive action.
- **OS/process exposure:** examples quote non-secret variables, use explicit
  argv, disable shell tracing around sensitive boundaries, avoid command
  interpolation and unbounded `xargs`, and use bounded polling. Secrets do not
  enter argv. Temporary evidence is permissioned, redacted, removed, and never
  committed or uploaded by default.
- **Network/runtime exposure:** runbooks observe the current private GKE/EKS,
  NetworkPolicy, Workload Identity/IRSA, Cloud SQL/RDS, and ingress boundaries.
  They must not open public cluster endpoints, firewall/security-group rules,
  database ingress, or weaken TLS/hostname verification merely to make a
  diagnostic command work.
- **Error envelopes and evidence:** public `/health/` and application APIs keep
  the existing coarse/sanitized response envelopes and request IDs. Detailed
  cloud, Kubernetes, SQL, and MCP errors remain in access-controlled operator
  logs, with bounded redacted excerpts when escalation requires them. Public
  issues and handoff docs use synthetic identifiers and never disclose private
  pack, tenant, domain, topology, credential, or operational evidence.
- **Persistence and concurrency:** PostgreSQL domain rows, immutable operation
  inputs/results, outboxes, provider resources, and Terraform state each have
  different owners. Recovery preserves row locks, generation/lease fences,
  idempotency, at-least-once delivery, soft-delete semantics, and audit. Redis,
  WebSockets, log streams, cloud task status, and Terraform output are not
  substitutes for authoritative domain state.

## Whole-repository surfaces in scope

The runbook authors must inspect and link the current versions of:

- `docs/index.md`, `docs/ops/**`, `docs/dev/**`, and `docs/qa/**`;
- `mkdocs.yml`, `.github/workflows/docs.yml`, the Vale workflow, ADR-022,
  ADR-038, `docs/adr/documentation-coverage.yaml`, and the existing
  documentation coverage check before adding any new documentation validator;
- `.github/workflows/deploy.yml`, `_gcp-dev.yml`, `_shifter-platform.yml`,
  `_shifter-engine.yml`, and provider destroy workflows where relevant;
- `platform/terraform/gcp/**`, AWS environment roots, `modules/log-aggregation`,
  `modules/ecr`, RDS/Cloud SQL, messaging, identity, storage, ingress, and alert
  modules;
- `platform/charts/shifter/**` and `platform/k8s/gcp/**` for actual workload,
  health, service-account, migration, secret-reference, and policy names;
- `shifter/shifter_platform/{cms,engine,management,mission_control,shared,config}`
  service, schema, command, error, audit, upload, lifecycle, and health seams;
- `shifter/engine/provisioner/**` for provider cleanup, naming, redaction,
  timeout, and result behavior;
- `shifter/installation/**`, `scripts/bootstrap/**`, `scripts/gcp/**`, the
  post-deploy smoke, RDS pending-modification checker, and `mcp/ops/**`.

The runbook portion should not modify those application runtime surfaces. The
expanded GEN-002 portion is limited to the repository governance, CI, test
seams, and existing generated connector templates named in the revised plan. If
a command in a runbook requires an application runtime or cloud capability
change to be safe or true, stop at a documented gap and scope that change
separately.

## Gotchas and anti-patterns

- Do not copy the old issue's Step Functions, provisioning Lambda, cleanup
  Lambda, ALB/EC2, RDS, S3, SQS, ECR, or Firehose nouns into a GCP procedure.
- Do not make one provider's resource name, error code, IAM model, or retry
  semantics the provider-neutral contract.
- Do not conflate launch dispatch, provisioner execution, result application,
  CMS projection delivery, and user notification; each can fail independently.
- Do not conflate a failed/destroyed database status with verified provider
  cleanup, or a running cloud resource with a safely classifiable orphan.
- Do not replay DLQ messages in bulk before inspecting a bounded sample,
  fixing the consumer/capability cause, and proving idempotent handling.
- Do not delete cloud resources, object-store keys, DB rows, Terraform state,
  container digests, queue messages, or identity accounts as a first diagnostic
  step. Quarantine/preserve evidence and use the owning cleanup service.
- Do not run raw SQL updates to range status, upload usage, user activity, or
  migration tables. Read-only SQL is a last-resort diagnostic through the
  existing gated operator surface; domain mutation belongs to services.
- Do not assume a dry-run, confirmation prompt, rate limit, or approval gate
  makes the underlying operation transactionally or semantically safe. In
  particular, do not execute legacy orphan reconciliation that can terminate
  provider resources before several direct SQL updates and cannot record
  independent cleanup verification.
- Do not use `kubectl exec` into the web pod as a generic administration plane
  when a dedicated worker, migration Job, management command, or workflow owns
  the operation.
- Do not treat a reusable workflow's input support as an approved top-level
  deployment route, or command existence as an approved operator transport.
- Do not conflate the log-aggregation SQS/DLQ, the platform lifecycle-event
  queue/DLQ, and the separately owned CloudTrail/XDR connector queues. Their
  payloads, consumers, replay safety, and authorities differ.
- Do not duplicate existing ops docs, validators, command builders, exception
  hierarchies, status enums, DTOs, log formats, or workflow logic in Markdown
  snippets.
- The accepted `test_operations_runbooks.py` contract is deliberately narrow:
  stable catalog inventory, required headings, index reachability, escalation
  presence, and rejection of a bounded list of unsafe shell-example classes. It
  complements review and shell tooling; it is not a general shell validator.
  Keep it in the existing ADR-guard unittest suite and keep ADR-022 enforcement
  evidence synchronized rather than creating another checker or manifest.
- Do not claim a configured backup, alarm, sink, lifecycle policy, or health
  check has been tested live. State the source-defined control and the separate
  readback/rehearsal needed.
- Do not copy recovery commands from an older operations document without
  checking them against the current architecture handbook, Terraform, charts,
  and workflows; the existing AWS disaster-recovery material includes legacy
  control-plane assumptions.
- Do not turn a temporary incident workaround into Terraform drift. Feed a
  durable prevention change back through the canonical owner and normal review.
- Do not use `latest` image tags, unbounded waits/retries, blanket role grants,
  disabled TLS verification, public cluster/database access, or broad log
  exports as recovery shortcuts.

## Non-goals and implementation boundary

- No issue implementation or 22 runbook bodies are delivered by this
  preflight, and this note is not an implementation plan.
- No new operator API, diagnostics endpoint, MCP tool, management command,
  reconciler, cleanup policy, alert, log sink, schema, exception hierarchy,
  persistence model, or cloud abstraction is introduced.
- No second documentation manifest or runbook-specific policy framework is
  introduced. The runbook catalog does not need a new ADR-022 feature entry
  unless maintainers separately classify it as a major platform feature; its
  pages remain discoverable through `docs/runbooks/index.md` and
  `docs/index.md`.
- No legacy orphan tool is qualified or refactored, no production deployment
  entry point is created, and no on-demand reconciler transport is added.
- No live tenant inspection, credential use, deployment, restore, migration,
  message replay, image deletion, range cleanup, or other environment mutation
  is authorized by writing the docs.
- No claim is made that GCP exports logs to XDR, that Artifact Registry cleanup
  is automated, that Cloud SQL PITR/maintenance-window handling is qualified,
  or that AWS and GCP incidents have identical recovery procedures.
- No private scenario content, tenant identity, infrastructure details, logs,
  domains, repository references, or acceptance evidence belongs in the public
  runbooks.

The documentation portion must pass `mkdocs build --strict`, the repository's
Vale workflow/check, and the architecture guard. The authoritatively expanded
GEN-002 portion must also pass each affected stack-native checker, the full
ADR-guard unit suite, all-files pre-commit, the configured pre-push review, and
hosted CI. No validation debt is converted into a renewed exception.
