# Blocking configuration security scans: architecture preflight (#2558)

Status: pre-implementation guidance. Date: 2026-10-27.

The supplied issue #2558 is the contract; no formal requirement is attached.
This note records boundaries and design hazards, not an implementation plan.
Inspection found a clean worktree: the patch mentioned in the issue was not
present as local changes, and its reported finding counts are not verified here.
No scanners, cloud inventories, or deployed workloads were exercised by this
preflight. ADR-004, ADR-006, ADR-007, ADR-017, ADR-020, ADR-037, ADR-043, and
ADR-044 already supply the architecture; no new abstraction or ADR is needed.

## Decisions that constrain implementation

**Both configuration scanners must fail closed.** Keep Terraform Checkov,
Kubernetes Checkov, Trivy configuration scanning, and exact-release image
scanning as distinct policy surfaces. Their findings and exemptions are not
interchangeable. Preserve the current scan roots and rule coverage, pinned
scanner actions/binaries, checksum verification, and least-privilege CI.
Neither scanner failures nor missing/incomplete scans count as a clean result.

The Trivy target loop currently uses `set -e`: changing exit codes alone would
stop on the first failing tree and skip later trees. Reuse the status aggregation
pattern in the neighboring OSV job to scan all targets and return failure after
collecting results. Retain failure diagnostics through the existing artifact
step's failure condition. Do not introduce a scanner framework, unconditional
success, severity reduction, target omission, or another advisory wrapper.

Workflow policy also exists outside `_quality.yml`:

- `scripts/adr_guard/_guard/checks/quality_ownership.py` explicitly classifies
  `security-k8s` and `security-trivy-advisory` as evidence-only jobs.
- `.github/quality-path-filters.yaml` and
  `scripts/quality_ownership/{contract,classify_paths}.py` own routing and
  required lint/security/test coverage; workflow edits force the full matrix.
- `_deploy_workflow_plan_scope.py` owns security-job immunity to `skip_tests`;
  its explicit job list includes Checkov k8s but currently omits Trivy config.
- ADR-004-R11 in `docs/adr/index.yaml` and `docs/technical/dev/adr-enforcement.md`
  still describe Kubernetes Checkov as advisory. Update that policy with the
  implementation, preserving the separate canonical Terraform policy at
  `platform/terraform/.checkov.yaml` and its invocation-parity checker.

Reconcile these incumbents and their tests with the new blocking posture.
If job/check/artifact names change, reconcile callers and required-check
references too; retaining an identifier does not justify misleading advisory
policy. Do not change OSV, gitleaks, Sonar, or the release image gate incidentally.

**UID migration is an image/runtime/admission contract.** The intended first-party
runtime identity is 10001, with matching group and volume ownership where that
image runs. Updating Deployment YAML alone is insufficient. The affected
incumbents include:

- The portal, provisioner, and preparation Dockerfiles, their users/groups,
  `HOME`, copied files, and writable directory ownership.
- Chart `security.app`, `security.guacd`, and `security.guacamole` values,
  `values.schema.json`, `_helpers.tpl`, and supporting GCP base manifests.
- `shared/cloud/kubernetes/_profile.py`'s
  `standard_provisioner_hardening()` and `_job_manifest.py`'s container and
  pod `fsGroup` rendering, shared by GKE and EKS.
- `shared/cloud/preparation_{runtime,installation,policy}.py`, including the
  separate preparation controller/worker and generated CEL policy.
- `shared/cloud/feature_artifact_jobs.py` and the chart's
  `feature-artifact-acquisition.yaml` policy, which run the portal image.
- `installation/gcp_migrations.py`, the migration/smoke renderers under
  `scripts/gcp/`, and bootstrap-generated identity, probe, migration, and
  Guacamole-provisioning Jobs in `scripts/bootstrap/aws_eks.py`.
- Both provisioner admission policy representations under the chart and GCP
  base: they currently pin `fsGroup == 1000`. Preparation and acquisition
  policies separately pin `runAsUser` and `runAsGroup` to 1000.

Here, `shared` means `shifter/shifter_platform/shared`; `installation` means
`shifter/installation`. These are compatibility obligations, not permission to
change unrelated numeric values. Guacd and Guacamole currently use upstream
1000/1001 identities; resolve any scanner finding with compatible image user,
group, and file ownership, including mounted homes/drives. Preserve the Cloud
SQL proxy's separate 65532 identity. The chart schema currently accepts any
positive UID/GID; that shape check alone does not prove compatibility with the
image or exact admission profile. Never broaden CEL to arbitrary non-root
users, permit both old/new profiles indefinitely, add a privileged chown init
container, or relax restricted PSS to make the migration pass.

Admission changes affect already admitted and queued work. Account for running
Jobs, retries, cleanup, old image references, and rollback through the existing
operation lifecycle; do not rewrite immutable operation inputs or delete their
ownership records. Retain exact per-Job Secret and create-or-observe semantics.

**Worker readiness consumes existing heartbeat ownership.** `run_worker`,
`run_ctf_scheduler`, and each specialized loop command already own their
heartbeat paths and cadence, consumed by base/chart liveness probes. Readiness
must check that workload's actual progress with bounded staleness, tolerate
startup/long operations according to its existing heartbeat semantics, and fail
for absent/stale files. A live PID or always-successful exec is not readiness.
Readiness gates rollout availability; it does not stop queue consumption or
restart a failed worker. Preserve liveness/remediation and shutdown/lease
handling. Do not add an HTTP health service, second heartbeat schema, or
readiness probes to one-shot provisioner containers whose admission forbids them.

**Guacamole credentials stay behind the existing hydration boundary.** Keep the
provider-owned secret stores and `guacamole-runtime` Kubernetes Secret, populated
by AWS/GCP bootstrap and the staged GCP workflow. Helm may reference a Secret
name/key and mount path; it must never contain credentials in values, history,
ConfigMaps, generated runtime configuration, or output. Retaining the existing
Secret key names avoids an unnecessary producer-side secret schema migration.

Mount the database keys read-only with explicit key projection and least
permissions readable by the effective non-root identity. Mount outside writable
`GUACAMOLE_HOME`, whose upstream initialization regenerates configuration. Remove
the database credentials from `envFrom` exposure; preserve the JSON-auth key's
existing contract through explicit projection if still needed. Do not leave an
env fallback that silently defeats the file-delivery contract. Secret contents
must not appear in command arguments, shell tracing, diagnostics, or long-lived
child environments. File delivery reduces transport exposure; the application
still necessarily handles the credential in memory.

Use the pinned upstream image's supported property/file mechanism, shared by
schema initialization and normal JDBC startup. Apache records a
[_FILE regression in Guacamole 1.6.0](https://issues.apache.org/jira/browse/GUACAMOLE-2119);
the current repository uses a rebuilt digest of that webapp. Thus `_FILE`
variable names alone are not proof of working integration. Verify the exact
image's property-file support, username alias, entrypoint ordering and Java
startup without converting the mounted secret back to a long-lived exported
password. The wrapper currently exports `PGPASSWORD` for `psql`; use libpq's
protected password-file mechanism where needed, with correct escaping and
cleanup, rather than a password-bearing connection URI or argv. Missing,
unreadable, empty, or malformed credential files must fail without revealing
contents. Keep existing idempotent initialization and `ON_ERROR_STOP`; connection
or authorization failure must not masquerade as an absent database schema.
Account for startup-only reads and restart requirements when credentials rotate;
a `subPath` mount does not provide ordinary projected-Secret refresh behavior.

## Cloud and exception boundaries

| Concern | Existing owner and required boundary |
| --- | --- |
| CTFd root encryption | `platform/terraform/modules/portal/ctfd/{main.tf,ctfd-userdata.sh.tftpl}` owns the standalone instance. Its root is deleted on termination, and bootstrap freshly clones CTFd and generates a new signing key. Treat encryption as a data migration before replacement: preserve database/Compose volumes, uploads, configuration and signing material, require consistent backup and verified restore, encrypted recovery storage, downtime/cutover and rollback evidence. Do not equate new-instance bootstrap, `create_before_destroy`, or `encrypted = true` with preserved application state. AWS documents [snapshot/volume encryption migration](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-examples.html); a scanner pass does not establish live encryption. |
| General SG egress | The issue identifies #860 as the real refactor. Ordinary management/build/SSM/API/update egress must be addressed through its owning Terraform/network design; a description or temporary blanket waiver does not close the finding. If the dependency is not delivered, report the acceptance gap. Inventory IPv4/IPv6, DNS, private endpoints, return traffic and bootstrap/deploy/runtime dependencies rather than copying a 443-only rule everywhere. |
| Range and NGFW egress | ADR-017/ADR-020 and `platform/terraform/modules/range/vpc/` distinguish local SG reachability from route/firewall internet containment; NGFW dataplane forwarding is product behavior. Do not break either to satisfy a generic SG rule, or use those rationales for unrelated control-plane SGs. Any resource exception must demonstrate its actual route/default-deny or dataplane role, including provider/profile conditions, and preserve the existing SG/firewall checks. |
| GCS CMEK | Inventory the scanned bucket resources in `portal/gcs`, `packer-build-infra`, `raes-package-storage`, and foundational `cicd-oidc-identity`. Reuse the rotation, `prevent_destroy`, exact service-agent grant and dependency pattern in `portal/artifact-registry`, with storage-specific identities. Key location must match bucket location, not blindly `var.region`; grant the bucket project's Storage service agent access before use. Keys must outlive retained/versioned data. The foundational evidence bucket must not depend on platform-core keys that bootstrap cannot yet create or destroy safely. Include API enablement, bounded deploy IAM and provider validation. A [bucket default key affects future writes](https://docs.cloud.google.com/storage/docs/encryption/using-customer-managed-keys), not existing objects: declare and verify the live/noncurrent object migration boundary separately, respecting retention locks and evidence immutability. Verify upload/download, image export/import, package reads, and usage-log delivery under the actual service identities. |
| Packer bucket grants, GCP-0007 | `packer-build-infra/main.tf` grants image-object admin, legacy bucket reader, and scratch-bucket storage admin; it already separates project-level metadata discovery into a narrow custom role. Derive the necessary object create/read/list/delete and bucket metadata permissions from build/export/cleanup, scoped to the two owning buckets. Do not replace admin with a broad project role or invent a role name that merely hides equivalent privilege. Preserve the purpose/prefix-conditioned create-only evidence grants in `cicd-oidc-identity`; image writers must not gain evidence overwrite/delete. |
| Cloud SQL logging | `portal/cloud-sql/main.tf`, the #2084 closure note, its ADR-004-R11 exceptions, and `TestGcpReleaseSecurityClosure` in `scripts/adr_guard/tests/test_deploy_workflow.py` own the existing confidentiality decision. **Verify the finding identity:** Aqua documents [AVD-GCP-0014 as temporary-file logging](https://avd.aquasec.com/misconfig/google/sql/avd-gcp-0014/), not all-statement logging. Reconcile the pinned scanner's actual ID, rule definition, and resource before fixing or waiving anything. `log_temp_files` metadata and `log_statement` SQL text are different settings. PostgreSQL documents [the logging distinctions](https://www.postgresql.org/docs/current/runtime-config-logging.html), including duration-only logging and statement emission on errors. Inspect effective defaults, error-statement thresholds, parameters, audit settings, and emitted spill/error logs: enabling one metadata flag must not cause accompanying SQL text to leak. Preserve #2084's no-SQL-text decision; a different verified statement-logging finding needs its exact confidentiality rationale, never an automatic exemption based on a mismatched ID. |

Do not conflate an intentional functional need with a scanner parsing error.
For CKV_K8S_43, a narrow placeholder exception is justified only by evidence that
every affected deployment path replaces it with the verified attested digest;
the base manifest alone is not deployable proof. Reuse `_gcp-dev.yml`'s verified
digest rewrite, bootstrap digest validation, chart image schema and ADR-037
provenance. Unrewritten placeholders, mutable deployed tags, or an unverified
digest are not covered. For CKV_K8S_38, the dedicated provisioner launcher
requires API access under ADR-006-R4; preserve its exact SA/RBAC/namespace/network
boundary. Ordinary workers and provisioner execution Jobs must keep automount
disabled. Any other API-using controller needs its own demonstrated need.

Use resource/workload-scoped scanner annotations with factual rationale and a
matching `docs/adr/exceptions.yaml` entry (existing rule ID, owner, expiry and
paths). Validate scanner-specific syntax with the pinned version; one scanner's
annotation does not exempt another. Keep Terraform Checkov configuration and
the existing `.trivyignore.yaml` policy distinct: that file currently governs
exact-release image CVEs and contains no misconfiguration waivers. Do not add a
global misconfiguration skip list, weaken the immutable-subject compatibility
adapter, or call known remediation debt a verified false positive. This
preflight grants no new exceptions.

## Cross-cutting contracts and verification surface

| Layer | Canonical incumbent and pass condition |
| --- | --- |
| Installation/config shapes | `installation/{schema,registry,render,runtime_inventory_aws,runtime_inventory_gcp}.py`, `scripts/gcp/render_runtime_env.py`, portal `config/_env_manifest.py` and generated manifest, provisioner `config/_env.py`, chart `values.schema.json`. Extend only an owning schema when a new deployment input is necessary, propagate it through its existing renderer/inventory, and preserve strict shapes and non-secret references. Guacamole-local file paths do not belong in portal/provisioner env inventories unless those processes consume them. |
| Auth, policy and admission | Existing backend-specific IRSA/Workload Identity, service accounts, RBAC, provisioner/preparation/acquisition CEL policies, PSS and NetworkPolicies. UID/file changes grant no new Kubernetes, DB, tenant or cloud authority; server-side admission must accept the intended generated Job and reject altered identity, image, env, command and mounts. |
| Secret transport and OS | Existing bootstrap secret-stdin helpers, protected temporary-file cleanup, provider Secret Manager hydration, `shared/cloud/sensitive_env.py` and Kubernetes `_secrets.py`/`_job_manifest.py`. `_FILE` is a pointer, not permission to place a value in it. Preserve per-Job sensitive-env handling for other workloads. Prove effective UID/GID/fsGroup, read-only mounts/root, writable scratch bounds and file modes; keep secrets out of process argv, `/proc`-visible child env, Helm history, logs and public artifacts. |
| Errors and observability | `shared/cloud/exceptions.py`, `shared/errors.py`, `shared/api/errors.py`, `shared/log_sanitize.py`, provisioner `log_redact.py`, installation/bootstrap failure boundaries and shell nonzero exits. Reuse bounded errors and envelopes; never return raw Kubernetes/provider/psql output or credential/config contents. Missing evidence is failure/unknown, not success. Existing heartbeat/rollout signals and safe scanner rule/path summaries suffice; no logging framework or exception hierarchy is needed. |
| Persistence and workflow | Existing operation envelope/outbox/Job ownership and cleanup under ADR-043; Terraform state ownership and moved-resource conventions; bootstrap/Helm deployment and release evidence under ADR-007/ADR-037/ADR-044. Do not introduce Django persistence or a migration controller for configuration findings. Keep private release SARIF/SBOMs in the established evidence store; existing config-scan artifacts must contain only permitted first-party source diagnostics, never secrets, live plans, private pack content or raw operational evidence. |

The extensibility seams already exist: typed hardening profiles carry runtime
UID/GID and writable mounts, with attested image references supplied through the
existing task/deployment inputs; chart values carry deployment-specific Secret
names and security identities; Terraform module inputs carry bucket/key/location
bindings within the owning state. Use these seams and existing heartbeat
intervals rather than scattered new constants or arbitrary tenant overrides.
Do not turn fixed executable security contracts into general user configuration.

Verification must exercise the pinned scanner invocations after the prerequisite
patch is present, all supported chart values/renders and Kustomize roots, and
negative failure cases (scanner error/later failing target, stale heartbeat,
unreadable secret, mismatched image/UID/admission). Reuse existing chart tests,
bootstrap/GCP renderer tests, `test_gcp_job_launcher_{manifests,rbac}.py`, neutral
Job/preparation tests, Guacamole entrypoint and native-RDP integration tests,
and `TestGcpReleaseSecurityClosure`; test real process/network boundaries per
ADR-019. Structural checks alone cannot prove credentials work or data survived.

Required implementation checks remain the full ADR guard, actionlint, Terraform
fmt/validate/native tests and recursive tflint, Helm lint/template, kube-linter,
kubeconform and applicable Python/import checks from `AGENTS.md`. The PR body
must name each live impact: image/identity and admission rollout, secret
projection/restart, CTFd replacement/data restoration, egress connectivity, KMS
IAM/key/object migration, Packer grant changes, and verified SQL logging changes
or exceptions. Distinguish code validation from actual live readback.

## Non-goals

This preflight implements no finding, changes no scanner posture, accepts no
risk, applies no infrastructure, and performs no live migration or deployment.
It introduces no requirement, new service, hosting destination, secret store,
job runner, schema family, exception hierarchy, or workflow orchestrator.
It does not redesign range egress outside its owning #860/ADR boundaries,
remove functional NGFW forwarding, replace Helm with raw manifests, change
tenant/participant authorization, or copy external pack content into core.
The implementation cannot claim complete acceptance while a real finding,
required migration, or dependency remains unresolved.
