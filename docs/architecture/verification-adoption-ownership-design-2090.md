# Verification and adoption ownership design (#2090)

Architecture preflight and design, 2026-10-27. Inspected source revision:
`4b44a428569ec5fc969bdc8fa3a15b636b129dac`.
The issue is the delivery contract; no requirement UID is attached.
This note applies existing decisions. It is not an implementation plan, and it
is not evidence that the reconciliation is delivered.

## Decision and authority

Keep [the testing guide](../dev/testing.md) as the single published command
matrix. Root `Makefile` targets remain local entrypoints; package configuration
owns runner policy; CI owns enforcement. Other contributor and workflow docs
link to that matrix instead of maintaining another command list. Do not add a
command registry, verification service, evidence database, or status schema.

Apply ADR-004-R24 (quality ownership), ADR-019 (boundary tests), ADR-022
(documentation), ADR-036 (browser security), ADR-055 (accessibility), and
ADR-069 (repository identity).
The [ADR registry](../adr/index.yaml), current source, and
[architecture handbook](../technical/architecture.md) take precedence over
historical preflights and review proposals. No new ADR or exception is needed.
The CSP enforcement that moved from #2085 amends ADR-036; its own design note
records that change.

## Findings that constrain the implementation

- Root `make test` runs a bounded set of no-service lanes. It now includes the
  SPA (`make test-spa`) and every MCP package (`make test-mcp`), which mirror
  their CI jobs. It does not run browser journeys, PostgreSQL, Redis, OpenFGA,
  the smaller support packages, policy, or deployed qualification. Some included
  lanes still need Helm. The matrix states each prerequisite.
- `make test-js` is the static-JavaScript Jest suite. The frontend package owns
  the React SPA's Vitest suite. Neither is Playwright, accessibility, or a
  deployed participant journey. Local browser targets use SQLite; CI uses
  PostgreSQL. Do not describe those environments as identical.
- CI's platform test job also tests/builds the independent SDK and runs Redis
  integration. Job names alone do not describe their full execution scope.
  SQLite coverage, broad PostgreSQL persistence tests, and OpenFGA integration
  deliberately overlap application code while proving different behavior.
- Local lanes match the CI command for the same package. The Packer lane runs
  its configured coverage floor, and the PostgreSQL lane runs static collection.
  Bootstrap and GCP scripts select unit tests locally; CI adds the `integration`
  marker. Do not claim parity from a shared package name or a Makefile comment.
- `make policy` includes the ADR guard, import-linter, diff checking and Vale;
  the separate configured lint command repeats the ADR guard. Avoid redundant
  calls inside one verification pass, while preserving required workflow
  boundaries. Its Vale selection uses committed `origin/dev...HEAD` changes;
  it does not establish that uncommitted or untracked prose was checked.
- The current contract already owns deployment utilities, guard tooling, stack
  smoke, container inputs, charts, Kubernetes and all four MCP packages.
  MCP lint uses `--max-warnings 0`; Kubernetes security ownership uses blocking
  kube-linter. Advisory Checkov/Trivy jobs are separate signals. Credit actual
  commands, paths and failure propagation rather than a job's name.
- [#1698](https://github.com/Brad-Edwards/bigrae/issues/1698) is closed. Its
  ownership-gap work landed through #2557 and #2560; its final report covers the
  later repository-identity change in #2559. Its body cites ADR-004-R23, but
  quality ownership is ADR-004-R24. No quality-path gap exception remains in
  `docs/adr/exceptions.yaml`. Reconcile the issue text to that evidence; do not
  reopen or close work by inference.
- The REV1 release-evidence index contains old ACES paths and gate descriptions.
  Reuse its distinction between gate posture and observed evidence, not its
  inventory as current truth. Current realization is RAES; ADR-068 parks the
  broker. Retired paths and parked designs must not become adoption blockers
  merely because an old review listed them.
- Adoption issue state and native edges disagree in two places. #2085 is closed,
  but CSP stayed report-only; the maintainer moved that work into #2090 (see the
  [CSP enforcement design](browser-csp-enforcement-design-2090.md)). #1910 is
  closed but still blocked by open #2130 and #2131; those two edges are removed
  because #2091 and #681 track that work.
- 32 `DRAFT` requirements carry `IMPLEMENTS` links, which only `ACTIVE`
  requirements accept. Many link descriptions state that behavior is absent,
  but the current code implements most of it. Each link is checked against the
  current code. 31 requirements link to code that implements part of the
  requirement; they move to `ACTIVE`, which means implementation started, not
  delivery. CTF-506 stays `DRAFT`: teams still have no join password.
- A link description states a code fact. Where the code still lacks the
  behavior, the link uses `DOCUMENTS`, not `IMPLEMENTS`. A stale absence
  statement is corrected to the current implementing file and symbol.
- Traceability paths that no longer exist follow git rename history. A module
  that became a package points at the package directory. A path with no
  successor is removed. No new implementation link is inferred.

The #2080 review findings, backlog and verification record were read from the
review branch. Native blocking edges were read through the issue-dependency API.

## Canonical incumbents and whole-repository scope

| Concern | Existing owner to reuse |
| --- | --- |
| Published guidance and entrypoints | `docs/dev/testing.md`, root `Makefile`, `README.md`, `CONTRIBUTING.md`, `docs/DEVELOPMENT_WORKFLOW.md`; distinguish the nested platform Makefile's Compose operations. |
| Dependency and test policy | Each package's `pyproject.toml`/`uv.lock` or `package.json`/lockfile, platform `frontend/vite.config.ts`, pytest markers, coverage sources/floors and warning filters; `sonar-project.properties` and actual Sonar gate evidence. |
| Ownership and routing | `.github/quality-path-filters.yaml`, `scripts/quality_ownership/{contract,classify_paths}.py`, ADR guard `quality_ownership.py` and its model/routing helpers, `scripts/check_layer_imports/layer_imports.yaml`. |
| Required execution | `.github/workflows/_quality.yml`, `deploy.yml` including its direct accessibility/PR Gate dependencies, `.pre-commit-config.yaml`, `.ground-control.yaml`, `.gc/plan-rules.md`. Staged hooks are hygiene, not the full CI estate. |
| Persistence and service evidence | Platform root `conftest.py`, `tests/conftest.py`, `config/{_runtime_env,_database_settings,_cloud}.py`, pytest-django; `scripts/run_openfga_integration.sh` under the platform package. PostgreSQL constraints, transactions and locks remain in existing models/services. |
| Browser and image boundaries | `frontend/playwright.config.ts`, `e2e/auth.setup.ts`, teardown and accessibility baseline; `scripts/stack-smoke/`, Docker build contexts, chart profile tests, native Kubernetes/Terraform validators. |
| Live producers | Existing `run_post_deploy_smoke`, `scripts/smoke-test*.sh`, `_gcp-dev.yml`/`_shifter-platform.yml`, `cms/raes/validation.py`, `run_raes_backend_validation`, and `uat/range-functional-smoke/`. Image/preparation qualification retains its owning producer and digest scope. |
| Governance and records | `docs/adr/{index,exceptions,documentation-coverage}.yaml`, ADR enforcement docs, current handbook, Ground Control tools and canonical GitHub issues/run records. |

The upcoming matrix must describe prerequisites, working directory, selected
packages/markers, service/backend posture, coverage ownership, CI counterpart,
effects and the limits of the proof. Aggregate rows explain which rows they
already include; a reader must not have to run both an aggregate and each child.
Separate required service/backend evidence from redundant execution in the same
posture. Preserve package-owned thresholds and warning policy.

## Cross-cutting layers the design must pass

| Layer | How the intended design satisfies it |
| --- | --- |
| Repository and tool authorization | Resolve GitHub operations from `.ground-control.yaml` through repository-bound tools; ADR-069 separately owns runtime self-identity, immutable upstream IDs and deployment trust. A renamed repository does not rename the Ground Control project or requirement UIDs. Use supported requirement/traceability/dependency tools, retaining their authorization and error responses. |
| Ownership schema and policy | Reuse `contract.py`'s safe YAML loader, closed keys/types, `ContractError` and `UnknownPathError`. The classifier rejects unknown/ambiguous paths before outputs. The ADR guard checks tracked estate, all three responsibilities, real `if`/`needs` reachability and MCP matrix membership. An advisory job cannot discharge a blocking responsibility. |
| Test environment and binding shape | Establish posture before settings import. Reuse `_runtime_env`, `_database_settings`' closed `sqlite`/`postgres` selector and existing `DB_*` fields, `_cloud`'s local-provisioner fence, and `_env_manifest.py`/`env-manifest.json` if bindings change. No new settings module, parser or env alias. For non-pytest management commands, state test posture explicitly. |
| Database lifecycle | pytest-django owns application test databases and xdist isolation. Preserve the root PostgreSQL guard requiring executed, unskipped marked evidence and actual PostgreSQL behavior. OpenFGA's dedicated harness owns its containers and SQL duties. Never aim synthetic credentials or destructive test setup at a live database. |
| Browser authentication | Preserve normal Django/CTF sessions, CSRF, role authorization, loopback-only PR target validation, synthetic seeding and authenticated-state teardown. `dev_login` is a development path. A live target uses the functional harness's `RunProfile`, HTTPS and real participant authority; changing `SPA_E2E_BASE_URL` cannot convert the PR lane into a deployed lane. |
| Secrets and OS exposure | Real tokens/passwords/cookies stay out of command argv/URLs, shell tracing, remote configuration and reports. Use approved environment bindings or existing mode-0600 session-file inputs, never read secret stores to populate examples. The existing test keys are synthetic. Preserve existing HTTPS Guacamole transport without printing its token-bearing URLs. Do not substitute real secrets into disposable harness templates, dump environments or expose local services beyond their intended boundary. |
| Live runtime/config validation | Refer to installed image digests and the existing `render_runtime_env.py`, installation validators, `render_smoke_job.py` shape checks, Kubernetes admission/RBAC and CMS/Engine service boundaries. Do not document direct backend invocation as product-path evidence or bypass secret hydration, installed identity, backend selection or cleanup ownership. |
| Errors and observability | Preserve nonzero command status, classifier errors, Django `CommandError`, and the functional harness's `ProfileError`/`SessionError` and bounded report. An MCP `isError` result is failure even if transport succeeded. Existing API checks keep `shared/api/errors.py`/schema envelopes, authorization and shared audit; documentation needs no new exception hierarchy or telemetry. Reuse native JUnit/coverage summaries, `shared/log_sanitize.py` and producer-authored redacted details. Raw provider errors, terminal output, cookies and traces are not public evidence. |
| Guardrail and supply-chain enforcement | Keep existing pinned tools/actions, native scanners and import rules. Lint/security checks remain independent of test-skipping. Preserve fail-closed PR gating, coverage/warning ratchets and exact owner/expiry exceptions. Guardrail changes require matching ADR documentation and subsystem-native validation, not a waiver to make reconciliation pass. |

Local E2E/a11y targets remove `shifter/shifter_platform/db.sqlite3`; document
their disposable-state assumption and do not run them concurrently against that
shared file or an active development server. The policy target can install Vale;
dependency sync, image builds and cloud qualification have distinct prerequisites
and effects. No aggregate check may acquire a deploy, publication, promotion PR
or live-resource side effect. Live execution needs its existing authorization.

## Ownership, evidence and reconciliation boundaries

Quality-unit ownership means responsibility for blocking lint/security/test
execution. Human adoption ownership means the maintainer or issue assignee who
owns a remaining outcome. Requirement status, implementation merge, check
enforcement and live qualification are separate facts. The matrix and issue text
must preserve those meanings.

For each adoption claim, retain the owning issue/producer and exact source or
release revision, backend/profile and artifact digest where relevant, observed
result, evidence date, and residual limitation. Use existing issue prose/tables
and producer reports, not another machine schema. A source path proves existence;
a configured job proves a gate exists; a successful, correctly scoped run proves
execution. None alone proves live readiness, restoration or cleanup completion.
Post-deploy smoke is advisory on both current provider workflows; range readiness
and port reachability do not prove the functional harness's terminal exchange or
connected Guacamole session. An accepted destroy request does not prove teardown.

Ground Control project `shifter` keeps its requirements as repository files at
`docs/requirements/<UID>/requirement.md`. The frontmatter `status` and the
`## Traceability` section are the record. Changes to them land in a reviewed
delivery diff. The Ground Control workflow tools read those files and the issue
`Requirements` section. No requirement database or link API is used.
Read a requirement before correcting its UID/status or links. Only an `ACTIVE`
requirement accepts `IMPLEMENTS`; `DOCUMENTS`/`GITHUB_ISSUE` can associate tracking
work without claiming delivery. `TESTS` points to real relevant behavior, not
issue prose or a test-name match. Retain valid historical evidence when paths move.
Archival/deprecation is a lifecycle decision, not a synonym for "implemented".
No arbitrary requirement is attached to this requirement-free issue.

Use the supported native issue-dependency surface to reconcile blocker edges;
editing "blocked by" prose alone does not change GitHub's dependency graph.
Confirm the exact dependency obligation before removing a closed issue's edge.
Issue edits and link corrections must preserve valid existing scope, evidence and
remaining acceptance work. No closure, merge or public delivery claim follows
from this preflight.

## Extensibility and non-goals

The seam is the existing package/service posture: package metadata owns behavior,
Make selects local execution, and the ownership contract selects CI jobs and MCP
members (including shared-package fanout). A future package/backend extends those
existing inputs and validators; it does not add copied shell policy or another
ownership map. Live variation uses existing profile, environment and deadline
parameters rather than hardcoded provider, deployment or scenario identities.

This issue is guidance and evidence reconciliation. It does not redesign APIs,
DTOs, repositories, persistence, logging or exception hierarchies; introduce a
test framework; revive retired paths; change coverage floors; add duplicate
scanners or implementation-mirroring tests; or qualify/deploy a release. A real
missing check needs relevant behavior and failure-propagation proof using existing
guard/native-tool tests. Broader product defects retain their owning issues.
Private pack content, names, locations and operational evidence stay in their
owning repositories; public records use synthetic examples. Tool availability
does not authorize a new service, evidence upload or hosting destination.
