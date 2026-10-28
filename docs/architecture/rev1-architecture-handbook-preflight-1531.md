# REV1 Current-State Architecture Handbook Preflight (#1531)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.


Status: issue-scoped pre-implementation guidance; not current-system authority.

Reviewed: 2026-10-17, checkout `a2372dc60`.

Contract: supplied issue #1531 and active requirement GEN-001. ADR-022 already
enforces major-feature user/technical documentation; #1531 adds a current-state
handbook and architecture drift review. This note implements neither deliverable
and introduces no architecture rule, exception, or implementation plan.

## Authority and publication boundary

Build on `docs/technical/architecture.md` as the canonical public entry point,
linked from `docs/index.md` and `docs/technical/index.md`. Companion pages may
live under `docs/technical/handbook/` when needed. Keep one maintained explanation
of each boundary; existing subsystem guides should link to it or supply detail.
Do not introduce `technical/architecture/index.md` alongside `architecture.md`:
they compete for the same MkDocs output URL.

ADR-038, `mkdocs.yml`, and `.github/workflows/docs.yml` own the documentation
platform. Top-level `docs/` is public, static documentation. The former Django
documentation app is retired. `docs/architecture/` is excluded from the site;
putting the handbook here would fail publication. Do not remove that exclusion
wholesale: it also protects the publication boundary for design/review records.
Exclusion from MkDocs is not confidentiality; repository files remain visible.

Accepted, superseding ADRs define intended boundaries; implementation, mounted
routes, deployment inputs, migrations, and tests establish current behavior.
Record a disagreement as an explicit gap with evidence. Neither an accepted
proposal nor a historical preflight proves that behavior is deployed or qualified.
State the reviewed revision/date, distinguish current/transition/target claims,
and link canonical source rather than copying schema or configuration tables.
Code presence alone proves neither route activation nor production qualification.

The supplied GEN-001 links to `shifter/shifter_platform/documentation/docs/`
are historical: ADR-038 moved that content to `docs/`. The coverage manifest
already uses `docs_root: docs`; ADR-022's older narrative still says in-app.
Preserve GEN-001 and reconcile relocated artifact links through existing Ground
Control traceability during delivery. Do not recreate the retired tree, invent
a local requirements registry, or claim handbook completion from this preflight.

## Material traps in the existing documentation

- The REV1 review and GEN-001 preflight describe earlier states. Their parallel
  ACES rollout and authenticated in-app docs guidance are superseded. Preserve
  dated evidence, add visible historical status and a handbook pointer to
  preflights, and keep existing paths/anchors usable. A directory index alone
  does not warn readers arriving directly at a stale preflight. Durable design
  notes and accepted ADRs in the same directory are not automatically historical.
- The requested CyberScript-to-ACES state page must also explain the subsequent
  RAES hard cut. ADR-024/031/032 and `raes-migration-adr.md` govern; the platform
  pins RAES and environment-pack releases in `pyproject.toml`/`uv.lock`.
  CyberScript is retired. Do not revive aliases, dual readers, feature flags,
  import paths, or a reversible selector from old migration prose. Distinguish
  coordinated release/database rollback and narrowly versioned cleanup from a
  live compatibility path. A package rename does not prove semantic parity.
- `docs/technical/architecture.md` still describes ECS/GDC assumptions and
  provisioner-written domain state/outbox. Current provisioner dispatch uses
  Kubernetes Jobs on EKS/GKE; control-plane hosting, range substrate, guest
  execution, and CI runner placement are separate dimensions. Derive their
  supported combinations from installation bundles, runtime adapters, chart
  inputs, and backend manifests, not a single AWS/GCP table or legacy filename.
- The platform index still assigns audit to `management` and has an outdated
  package/model overview. ADR-045 puts durable audit under `shared`. Package
  classification and dependency allowances come from
  `scripts/check_layer_imports/layer_imports.yaml`, not the old five-domain list.
- Identity and model-access policies have later amendments. ADR-066 distinguishes
  Account, Principal, ResourceScope, and its activation boundary; ADR-068 parks
  the broker. Verify mounted routes, settings/composition, migrations and tests
  before labeling prepared adapters or dormant chart/code surfaces as live.
  Do not present older workspace defaults, staff/token rules, or broker design
  pages as universal current authority.

## Required views and canonical incumbents

Each arrow needs an owner, direction, contract, trust crossing and authority
level. Separate same-process Python dependencies from deployable/process
boundaries, commands from events, and authoritative state from projections.

| View/concern | Reuse and boundary to retain |
| --- | --- |
| Context and deployment | `shifter/installation/{schema,loader,registry,bundle_aws,bundle_gcp}.py`, runtime inventories/renderers, `scripts/bootstrap/`, `platform/terraform/`, `platform/charts/shifter/`, and relevant `platform/k8s/gcp/` assets. Cover browser/API, web/ASGI, domain workers, schedulers/reconcilers, outbox drainer/result applier, provisioner Jobs, Guacamole/guacd, isolated plugin/acquisition Jobs, cloud services and untrusted range guests. Describe optional/dormant surfaces as such. |
| Package/dependency ownership | The classification and per-symbol service allowlists in `scripts/check_layer_imports/layer_imports.yaml`, `.importlinter`, the `manage.py check_model_fks` command, and package manifests. Include CMS, Engine, CTF, Management, Workspaces, Mission Control, shared, config, independent provisioner, installation, client/adapter SDK, SPA, MCP tools and build/operator tooling. A Django app is not a deployable or necessarily a bounded business domain. Cross-domain models remain private; `config` composes public seams. Mission Control's permitted Engine data-plane access is not permission for lifecycle calls to bypass CMS. |
| Identity/tenant authority | `config` provider-auth adapters and settings, `shared.api.principals`, `shared.credentials`, `shared.authorization`, `shared.principal_port`, and owning `management.services`, `workspaces.services`, CMS/Engine/CTF services. Authentication, credential ceiling, policy decision, object ownership and tenant confinement compose; none substitutes for another. Keep customer/account, organization/workspace, CTF/event, range ownership, deployment and cloud IAM scopes distinct. Never derive authority from a browser selection or identity-provider claim alone. |
| HTTP/schema/errors | `config/api_urls.py`, `config/_drf_settings.py`, domain DRF serializers, `shared.api.{closed_serializer,strict_json,errors,contract}`, `shared.schemas`, `shared.exceptions`, and `shared.errors`. DRF/drf-spectacular owns the OpenAPI authoring surface; `shifter/shifter_platform/openapi/v1.json` is generated publication. Schema publication filters are not the mounted-route inventory. Link contracts rather than creating handbook DTOs, endpoint lists or another error hierarchy. Preserve one Django-origin session/CSRF and credential posture; static public docs and authenticated API schema/Swagger are different surfaces. |
| Commands/persistence/events | `engine.launch_intents`, `engine.operation_inputs`, `engine.services` result-apply/subnet-coordination seams, `engine/models/_operation_io.py`, `shared.operation_envelope`, `shared.operation_results`, `shared.messages.events`, and DB grants/migrations. Show operation reservation/input, dispatch, append-only results, generation/ownership/version validation, Engine application, transactional outbox, broker/domain projections, reconciliation and advisory WebSockets. PostgreSQL is durable truth; Redis/channel notifications and queue acceptance are not completion. Check each operation family and actual grants; do not declare all remaining direct SQL removed merely because ADR-043 targets that outcome. Audit is evidence, not a workflow queue. |
| Data stores | Owning models/migrations and deployment contracts describe PostgreSQL application/operation/audit state, Guacamole's separate persistence, object/artifact storage, Redis/cache/channel state, cloud queues/topics, secret stores and infrastructure state. Identify writers, readers, retention/recovery ownership and secret references. Do not invent a generic repository/data-access layer or equate cloud state with application truth. |
| Scenario/extension contracts | Only `shared.raes` interprets released RAES contracts; standalone provisioner readers validate plain serialized data. The platform's `shared/raes/backend-manifest.json` and its realization envelope bound capabilities. `shifter/shifter_adapter_sdk/`, `shared.scenario_verification`, `shared.cloud.runtime_plugins` and owning installation services already define distinct preparation, runtime and verification seams. Document exact version/entry-point/image binding and administrator authorization. Pack installation does not authorize executable code; isolated workers do not import into web/provisioner processes. Compatibility probes, unit conformance, guest readiness and live qualification are different claims. SDK source/buildability does not prove a published release. |
| Audit/observability | `shared.audit`, `config/logging.py`, `config/_logging_config.py`, provisioner `logging_config.py`/`log_redact.py`, outbox/reconciler health and provider monitoring. Show safe request/operation/generation correlation, retry/DLQ/recovery and audit ownership. Existing structured logging and error sanitizers are sufficient; add no handbook telemetry, log schema or evidence store. |

## Layers the documentation change must pass

| Layer | Required treatment |
| --- | --- |
| Public/security boundary | All site prose, diagrams, examples and linked artifacts must be suitable for public disclosure. Use synthetic identities and topology. Never read secret files, source dotenv, capture live settings/cloud output, or copy private pack details, logs, credentials, signed URLs or operational evidence. Reuse gitleaks and ADR-004's `no-live-cloud-identifiers`/`no-live-gcp-deploy-identity` checks; scanners do not replace editorial privacy review. |
| Auth/policy validators | The handbook adds no application route, controller or authentication scheme. Accurately trace existing credential resolution, service-owned policy/tenant checks, CSRF, WebSocket access and workload identities. Public static documentation is not a reason to widen `/api/v1/schema/`, Swagger, terminal, or administration access. |
| Configuration and env shapes | Runnable examples must pass `installation.loader.load_root_config`: duplicate/merge-key parsing, `RootConfig`, selected bundle settings and secret-reference validators. Follow `runtime_inventory*.py`, the existing bootstrap/runtime renderers, `config/_env_manifest.py` and generated `config/env-manifest.json`, then `config/_runtime_env.py` and owning settings validators. Helm examples also pass `platform/charts/shifter/values.schema.json` and existing chart contract tests. Use synthetic fixtures; do not hand-maintain generated env tables or imply dev/test defaults apply in production. `shared.cloud.sensitive_env.split_env` and `shared.cloud.kubernetes._job_manifest._build_env` require sensitive Job values to use `secretKeyRef`; config references are not hydrated secret values. Do not put credentials in ConfigMaps or Helm values, bypass a validator, or advertise unsupported backend combinations. |
| Markdown/browser/build | Use the configured Markdown/Mermaid extensions and existing static build; no new renderer, CDN, remote diagram service, executable snippets or raw HTML script. Check the built page, diagrams and navigation. MkDocs currently downgrades missing links/anchors to info, so a successful `--strict` build alone does not prove valid links. ADR-022 coverage checks existence/index reachability, not MkDocs exclusions or architecture accuracy. |
| OS/process and CI trust | Checks consume repo-relative paths and non-secret metadata using fixed argv; never shell-evaluate filenames or put tokens in argv, URLs, logs, artifacts or examples. New checks need no cloud credentials, live environment, new service or privileged runner. Preserve Docs PR build-only behavior and its existing Pages deployment permissions; ordinary docs-only routing must not hide guardrail changes from Quality. |
| Errors/diagnostics | Runtime examples follow `shared.api.errors`/`shared.errors` and bounded non-secret details, never raw provider exceptions. Documentation/guard failures use existing MkDocs diagnostics or `adr_guard` `Violation` and nonzero exits. Installation errors already use `ConfigIssue`/`InstallationConfigError`. Add no application exception family or raw-content diagnostic dump. |
| Persistence/observability | This issue changes source-controlled documentation and, if needed, its repo-native drift enforcement. It creates no database, migration, application audit event, log sink, telemetry service or retained execution evidence. |

## Ownership, drift and extensibility

Use the existing architecture maintainer in `.github/CODEOWNERS` for handbook
review ownership; its current rules do not cover the whole public technical
tree. Put the owner and reviewed baseline at the handbook entry, without
creating a competing personnel registry.

The drift check must connect changes in documented boundary contracts to review
of their handbook pages. Relevant triggers include package/service allowances,
public/shared wire contracts, auth/tenant composition, persistence/grants,
backend bundles/runtime inventories, deployable/chart contracts and extension
bindings. Internal implementation edits need not force meaningless prose churn.

Reuse `scripts/adr_guard/_guard/checks/documentation.py`'s existing documentation
pairing/coverage machinery, `docs/adr/documentation-coverage.yaml`, the PR ADR
impact convention, and `.github/quality-path-filters.yaml`/Quality routing.
Coverage currently checks feature docs, not source-to-handbook drift: do not
claim it already satisfies the acceptance criterion. A drift check should reject
a changed mapped boundary without its corresponding handbook review/update;
touching an unrelated doc is insufficient. Human review still checks meaning.

Keep manifest validity and change-sensitive review distinct. Existing CI runs
`adr_guard --all` with `files=None`; `get_changed_files` is staged/worktree scope,
omits deletions and does not represent a PR. Reuse the guard's existing read-only
Git/base-ref conventions and Quality's base/head context, with an explicit
comparison baseline. Account for deleted/renamed sources and pages, mapping
changes, and unavailable history; CI must report an unverifiable comparison
rather than silently pass. Preserve a useful local full-tree check. A removed
mapping must not erase the review obligation it had at the base revision.

The extension seam is a small declarative association from a boundary's existing
identifier/source paths to its published handbook page, alongside existing
documentation policy. Retain the JSON-compatible `docs/adr/*.yaml` convention
and `_load_json_yaml`/`Violation` reporting. Validate any added mapping's shape,
unique identifiers and repository-contained paths before matching/reading;
reject malformed entries and targets excluded by MkDocs. Existing coverage
normalization/index checks alone do not establish those properties. Resolve
links using the site's actual build rules; a disk path is not a published URL.
Reuse package and bundle identifiers; do not add a second
dependency, backend, human-owner or workflow registry, a source-code graph
generator, or embedded shell commands. A new backend/package should add its
association rather than require edits to several hard-coded classifiers.
If enforcement lands, amend the relevant ADR-022/002 documentation and registry
then; do not claim an unimplemented check in this preflight. Reuse guard tests
with synthetic diffs covering matching/unrelated docs, deleted/renamed targets,
invalid/removed associations, missing history, ordinary docs-only and guardrail
routing. Existing `DocumentationCoverageTests`, quality ownership/routing tests,
installation/runtime-inventory and chart tests are the relevant incumbents;
add tests only where behavior changes. Avoid prose snapshots.

## Public references and non-goals

Correct repository/support/citation links in README, CONTRIBUTING, SUPPORT,
SECURITY, CODE_OF_CONDUCT and published docs as applicable, using
`.ground-control.yaml`'s `Brad-Edwards/shifter`. Do not globally replace vendor
names: product references, copyright/license notices, historical authorship and
third-party attribution remain factual. The README's citation URL and local
CyberScript setup command are concrete stale examples. Do not invent a support
mailbox, SLA, affiliation, citation version/author, or private-advisory capability.
If a safe vulnerability-reporting route is not established, obtain a maintainer
decision before publishing a replacement; public issues are not its fallback.

This issue documents and checks the current architecture. It does not remediate
runtime gaps, perform a contract migration, activate prepared identity/model
features, redesign tenancy, change cloud/IAM/database privileges, introduce an
API/DTO/validator, move private content, bulk-delete history, regenerate frozen
evidence, or dispatch a publication/deployment. Release Please owns CHANGELOG;
do not hand-edit it to correct historical terminology.

Verification must include `python3 scripts/adr_guard/adr_guard.py --all --level ci`.
Public-site edits additionally use the existing local `mkdocs build --strict`
with `docs/requirements.txt` and changed-prose Vale policy; inspect links and
diagrams explicitly. Guard/workflow changes also require the existing guard
tests, documented ADR pairing and `actionlint` where applicable. Apply stack
checks only for subsystems actually changed; a documentation claim is not live
qualification evidence.
