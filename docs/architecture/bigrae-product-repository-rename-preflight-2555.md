# BigRAE product and repository rename preflight (#2555)

> Issue-scoped architecture guidance for #2555 under GEN-002. For current
> system behavior, see the
> [current-state handbook](../technical/architecture.md) and accepted,
> superseding ADRs.

Status: architecture preflight only. No product, repository, package,
runtime, cloud or persisted identity is renamed by this note. Issue #2555 owns
the naming cutover; GEN-002 requires architecture conformance throughout it.
This guidance applies ADR-003, ADR-004, ADR-008, ADR-009, ADR-037, ADR-040,
ADR-042, ADR-043, ADR-053 and ADR-066.

The [identifier ledger](bigrae-rename-inventory.md) and
[cutover guide](../how-to/bigrae-rename-cutover.md) record the no-deployments
premise. Consequently, state-preservation rules below constrain source contracts
and future-deployment templates; live cloud migration, cloud-trust overlap,
foundation inputs and workload rollback are not prerequisites. Do not provision
resources to manufacture that evidence. Supported published consumers and
GitHub repository configuration still require their own compatibility evidence.
This preflight inspects this repository only and makes no downstream-readiness
claim.

## Decision boundary

**BigRAE** is the canonical current product name and `bigrae` is the canonical
stem for new public coordinates. That decision does not make every occurrence
of `Shifter` or `shifter` the same kind of identifier. The implementation must
separate display branding, repository identity, published software coordinates,
configuration names, protocol discriminators, persisted identity and deployed
resource identity. A repository-wide textual replacement is forbidden.

The rename must maintain one reviewed identifier ledger. Each active occurrence
or deliberately grouped family has an owner and exactly one disposition:

| Disposition | Required meaning |
| --- | --- |
| `rename-now` | BigRAE is emitted and documented now; no supported reader or durable identity depends on the old value. |
| `compatibility` | BigRAE is canonical for new writes or authoring, while the old value remains accepted through one named owning boundary. The ledger records the removal milestone and evidence needed to remove it. |
| `permanent` | The old value is historical evidence, an immutable/persisted identity, or an external protocol coordinate whose mutation would be a different contract. The reason is explicit. |

The ledger must distinguish active source from generated files, lockfiles,
fixtures, historical migrations, release history and issue-scoped evidence. It
must record at least the exact identifier or bounded family, kind, owning file
or generator, consumers, disposition, compatibility owner, state impact,
security impact, removal milestone (when applicable), and verification evidence.
The acceptance gate is zero **unclassified active** occurrences, not zero raw
case-insensitive search matches. Historical migrations, changelog entries,
frozen contract snapshots and compatibility tests must not be rewritten to make
the search count look clean.

The ledger is [bigrae-rename-inventory.md](bigrae-rename-inventory.md), not a new
runtime schema. Family-level dispositions require repository-scoped evidence
that their membership is complete, including hidden policy/configuration files,
generators, package manifests and consumers. A prose family list alone does not
prove exhaustive coverage.

The default classifications are:

| Identifier class | Default treatment |
| --- | --- |
| Current UI copy, email copy, docs vocabulary, current examples, logos, page titles and current metadata descriptions | `rename-now` |
| Canonical GitHub repository slug, links, badges, repository variables, release metadata and source labels | `rename-now`, with exact-name trust consumers changed as one security cutover |
| Newly published packages, images, chart/release coordinates and CLI documentation | BigRAE canonical; old supported coordinates use `compatibility` until migration evidence and a removal milestone exist |
| Public config files, CLI names, environment keys and secret/reference names | BigRAE canonical authoring plus a bounded read-side compatibility path; conflicting old/new inputs fail closed |
| Wire discriminators, schema/version strings, webhook headers, export formats, UUID namespace seeds, persisted provider metadata and replayable envelopes | `permanent` unless a separately versioned dual-read migration is explicitly justified |
| Terraform addresses/state keys, bucket/object prefixes, database/role names, Kubernetes namespaces/service accounts, IAM role/custom-role IDs and cloud resource names | `permanent` by default; rename only with provider-supported move/import/adoption evidence and a no-replacement plan |
| Historical migrations, signed releases, changelog text, frozen schema snapshots, provenance records and issue/review evidence | `permanent` |

“Permanent” means intentional legacy identity, not current product vocabulary.
Current documentation may call it a legacy Shifter coordinate while presenting
BigRAE as the product.

## Concepts that must stay separate

- **Product name is not runtime authority.** Changing copy, a logo, an OCI
  title or an `app.kubernetes.io/part-of` value does not change tenant,
  authentication, authorization, secret, workload or resource authority.
- **Repository slug is not repository identity.** The GitHub name changes, while
  immutable repository and owner IDs remain security evidence. Redirects are a
  convenience for users, not proof for OIDC, provenance, trusted publishing,
  workflow routing or repository policy.
- **Source layout is not a published coordinate.** The `shifter/` directory,
  Python import packages, Python distribution names, Node package names, Helm
  chart name, OCI roots and CLI commands have independent consumers and cannot
  share one removal decision.
- **A display label is not a physical resource name.** Cloud display names and
  tags can often change safely. Terraform resource addresses, provider IDs,
  globally unique bucket/registry names, Kubernetes namespaces and IAM subjects
  can cause replacement or authority drift.
- **Configuration aliasing is not dual configuration.** Old and new names feed
  one parser/validator and one normalized internal value. There must not be two
  precedence systems, two generated env inventories or two secret payloads.
- **Metrics branding is not metric identity.** A namespace/type rename starts a
  new time series and can detach alarms, dashboards, SLOs and retention history.
  Keep the legacy metric identity or execute an explicit telemetry migration;
  changing a label in place does not prove continuity.
- **BigRAE product releases are not environment-pack distribution.** ADR-053
  remains binding. The rename must not introduce a package/catalog publication
  plane for RAES environment packs.

## Compatibility and persistence rules

Configuration aliases are accepted only at their existing owning ingress. The
BigRAE name is emitted by scaffolding, help and current docs. The Shifter name
may be read during the compatibility window. When both names are supplied,
equal normalized values are allowed only if the owner deliberately supports
that case; unequal values fail before mutation. Alias use must never print a
secret value or whole environment. Do not propagate both names beyond the
normalization boundary.

Preserve each owner's absent-versus-empty and default semantics. Compare secret
aliases without trimming, case folding or rendering their values; a naming
adapter cannot redefine the underlying setting's meaning. Apply only declared
aliases, not an environment-wide prefix rewrite.

This applies independently to the public root config and CLI
(`installation.schema`, `installation.loader`, `installation.cli`), bootstrap
inputs (`scripts/bootstrap` and workflow secret declarations), Django runtime
environment (`config/_env_manifest.py` and `config/env-manifest.json`), and MCP
ops policy (`.shifter.yaml` and `mcp/ops`). These are separate schemas. In
particular, renaming `shifter.yaml`, `.shifter.yaml`, `SHIFTER_CONFIG_*` and
`shifter-config` must not create a generic “product config” parser that
conflates deployment intent, repository automation and MCP authorization.

Published contracts keep their existing compatibility gates. The root/backend
bundle contract is generated through `installation.contract`,
`installation.registry` and `installation.publication`; the public HTTP API is
generated from DRF through `manage.py api_contract` and checked against
`openapi/v1.json`; shared wire models remain under `shared`. A renamed field,
enum, operation ID or discriminator is a compatibility change even when its
meaning is unchanged. Additive aliases or a new version must use the owning
contract's existing migration/version mechanism. Do not duplicate DTOs,
serializers, Pydantic models or validation functions under BigRAE names.

The following legacy identities are specifically high risk and cannot be
changed as copy:

- `shifter.provisioner-operation`, `shifter-ctf-content/v1`,
  `shifter-ctf-content-references/v1`, `shifter-challenges/v3`,
  `shifter-raes-completion-v1`, `shifter-provisioning`, the RAES backend/profile
  value `shifter`, and `X-Shifter-Signature` are wire or integration contracts.
- UUIDv5 seeds such as `shifter:range:*`, `shifter:personal-credential:*`,
  `shifter:ctf-notice:*` and `shifter:audit:v1:*` are identity algorithms.
  Changing the seed creates different identifiers for the same historical row.
- Terraform backend keys/prefixes, database and role names, secret IDs,
  provider metadata, object prefixes and cloud resource names locate existing
  state. A new spelling is a new address unless the provider proves otherwise.
- Kubernetes namespaces and service-account names participate in Workload
  Identity/IRSA subjects, NetworkPolicies, RBAC and validating admission policy.
  Helm labels also participate in selectors, PDBs and affinity. They are not
  cosmetic branding.

For Terraform-only address refactors, use checked-in `moved` blocks where the
provider object is unchanged. Physical-name changes require supported
import/adoption or an explicit data/resource migration and a saved plan proving
no unintended replacement, deletion or authority expansion. Never edit state
files, copy a state prefix, remove an address to silence a plan, or treat a
fresh-install success as upgrade evidence. Kubernetes immutable-field changes
need the equivalent mixed-version and rollback proof; deleting and recreating a
namespace, ServiceAccount, Secret or persistent workload is not a rename.

## Repository identity and supply-chain boundary

The repository slug cutover is one security boundary. Exact current-name
consumers include `.ground-control.yaml`, `.gc/plan-rules.md`, the root
`Makefile`, repository-gated workflow expressions, SonarCloud repository
variables, `gh attestation verify --repo`, Docker OCI source labels, reusable
workflow references, GCP WIF trust tuples, AWS GitHub OIDC subjects, self-hosted
runner registration, GitHub Pages links, Release Please, dependency tooling,
trusted publishers and the public adapter SDK documentation.

Reuse the existing `github_org`, `github_repo`, `github_repository_id`,
`github_owner_id`, `github_subject_format`, `purpose_contexts`, full protected
refs and workflow-reference checks in the GCP CI identity root. Repository and
owner IDs do not excuse a mismatched name claim; names and IDs must agree. AWS
trust remains on exact repository subjects at its existing role boundary. If a
bounded overlap is needed, it may admit only the exact old and new repository
subjects for the existing protected refs/environments and the same immutable
IDs; a repository wildcard, owner-wide principal, unqualified reusable
workflow, PAT, static cloud key or relaxed event/ref gate is forbidden.

Under the recorded no-deployments premise, future cloud trust uses one exact
configured repository name; introducing old/new cloud-trust overlap is
unnecessary. Existing profiles differ: the AWS deploy role in
`platform/terraform/global/iam/github-oidc.tf` currently permits
`repo:${github_org}/${github_repo}:*`, while its image role restricts protected
refs and GCP validates purpose/ref/Environment/workflow tuples. Preserve each
effective boundary and flag any needed hardening separately; do not claim the
AWS deploy profile already enforces GCP's narrower claim contract.

Attestation verification must bind each immutable artifact digest to the new
canonical repository after cutover. Existing artifacts and attestations remain
immutable evidence under their old repository identity; do not overwrite,
retag or rebuild them merely to change branding. Rollback refers to previously
verified digests, not mutable image tags. New image/package coordinates may
publish the same verified bytes only when their own provenance and admission
checks bind the new coordinate. A redirect or matching source SHA is not a
substitute for that proof.

Repository settings are external state. Required checks, branch protection,
rulesets, Environments, approvals, Actions permissions, Pages, security
features, deploy keys/apps, webhooks, repository variables/secrets, trusted
publishers, SonarCloud binding and runner registration require post-rename
readback. Organization transfer, ownership change and new hosting remain out of
scope.

## Executable governance boundary (GEN-002)

The rename applies existing decisions, not a replacement governance framework.
No new ADR, registry schema, runtime naming registry or policy engine is needed.

- **Governance identity stays independent.** A GitHub slug change does not
  rename the Ground Control `shifter` project, its immutable project ID,
  requirement UIDs, ADR/rule/check IDs or historical traceability. Keep the
  existing closed `.ground-control.yaml` shape; do not add branding or alias
  keys. Its `github_repo` selects repository operations. Its `sonarcloud` block
  is integration metadata, while `SONAR_PROJECT_KEY` and `SONAR_ORGANIZATION`
  repository variables select the scanner. Neither is derived from branding.
- **Registry validity is not execution evidence.** `docs/adr/index.yaml` and
  `exceptions.yaml` retain JSON-syntax YAML, existing required fields, unique
  IDs, typed interface contracts and referenced rule IDs. An accepted rule or
  empty `checks` list alone does not prove execution. Native tools and runtime
  validators still need their actual blocking invocation and behavioral proof.
  The ledger's compatibility milestones are not policy waivers. Any necessary
  exception uses the existing owner/reason/expiry and narrow path/check scope;
  expired entries cannot suppress failures.
- **One guard implementation.** Extend the owning `_guard/checks` family only
  where a changed invariant needs it, using `_common.Violation`, shared
  exception filtering, `_workflow_model` and its expression evaluator,
  `_registry.CHECKS`/`CHECK_LEVELS`, and the existing CLI/facade. Do not replace
  semantic workflow checks with matching comments or literal-name searches.
- **Routing must preserve live gates.** `.github/quality-path-filters.yaml`,
  `scripts/quality_ownership/contract.py` and `classify_paths.py` own fail-closed
  production-path classification. Every new package/alias path still needs
  blocking lint/security/test owners; workflow job/output names are routing
  contracts, not branding. Preserve `_quality.yml`'s independent
  `guard_selfcheck`, its live `adr-conformance` invocation independent of
  `skip_tests`, `deploy.yml`'s quality-result checks and governance-doc routing.
  Guard unit tests, a skipped job or advisory scans cannot substitute for live
  conformance. Sonar's intentional fork exclusion cannot silently exclude the
  renamed canonical repository or hide missing scanner variables.
- **Local and CI roles remain distinct.** Keep the documented fast local hooks
  and explicit full `make policy` boundary; moving or renaming a hook cannot
  remove its blocking CI owner. Reuse import-linter plus the package/layer
  inventory at `scripts/check_layer_imports/layer_imports.yaml`, Terraform's
  `platform/terraform/validation-inventory.yaml`, native IaC/chart checks,
  Dependabot package-root ownership and CodeQL configuration. A source-directory
  rename would require all of these consumers, CODEOWNERS, coverage/security
  paths and documentation generators to follow it; retaining source layout
  avoids that unrelated migration.

Observed baseline, 2026-10-25: the full CI-level ADR guard fails on five expired
exceptions (ADR-019-R1 and ADR-004-R24) and 23 uncovered quality responsibilities.
This preflight does not renew them, add exclusions or alter enforcement.
Their resolution belongs to the owning governance work; the rename cannot be
declared conformant while the required gate remains red.

## Canonical incumbents to reuse

| Concern | Canonical incumbent | Required reuse |
| --- | --- | --- |
| Product/ownership meaning | ADR-053 and `docs/architecture/product-development-surfaces.md` | Rename the backend; do not reintroduce a distribution surface or change OpenRAE ownership. |
| Identifier classification | The issue-owned reviewed ledger plus repository-scoped `rg`/generated-file inspection | One disposition per active identifier family; no second checklist hidden in a script or PR body. |
| Root deployment config | `shifter/installation/schema.py`, `loader.py`, `errors.py`, `contract.py`, `registry.py`, `publication.py`, `runtime_inventory*.py` | One safe YAML/Pydantic validation path, value-free `ConfigIssue` diagnostics, one generated published contract and one runtime-key inventory. |
| Django runtime config | Existing `config/_product_env.py` alias ingress, `config/_env_manifest.py`, generated `config/env-manifest.json`, owning settings modules and deployment renderers | Reuse the alias ingress; retain type/range/cross-field validation and generated-output ownership. |
| MCP operator policy | `mcp/ops/policy-config.js` (`loadPolicy`, `parsePolicy`, Zod schemas, ADR-014 semantic checks) and existing policy wrappers/audit | Keep the `mcp_ops` namespace and class/profile meaning. Alias selection must still pass strict shapes, capability gates, two-phase operations, secret handles and audit requirements. |
| HTTP/API contracts | DRF serializers/views, `config/api_urls.py`, `shared.schemas`, `shared.api.errors`, `openapi/v1.json` and ADR-040 compatibility gate | Branding changes do not rename or fork v1 paths, fields, security schemes, error codes or DTOs. |
| Authentication/authorization | `shared.api_tokens`, session/CSRF middleware, `shared.auth`, Identity Platform/Firebase verification, OpenFGA bindings and service-layer authorization | The product name grants no authority. Preserve issuer/audience/tenant checks, verified email/MFA, scopes and object/domain checks. |
| Secret handling | Installation `RequiredSecret` references, `runtime_inventory`, entrypoint hydration, Secret-backed Kubernetes values, GitHub Environment secrets and provider secret stores | Rename references only through the owning renderer. Do not duplicate, log, commit, place in argv or copy secret payloads between names. |
| Durable application state | Django migrations/models, service transactions, operation envelopes, outbox/reconciliation and historical UUID seeds | No bulk string rewrite, migration-side clone of service logic, replay-key change or delete/recreate shortcut. |
| Terraform/cloud state | Existing Terraform roots/backends, `moved.tf` precedent, bootstrap renderers, saved plan/policy gates and provider readback | Preserve backend locations and physical IDs by default; prove imports/moves and zero unintended replacement before apply. |
| Kubernetes/Helm | `platform/charts/shifter`, `platform/k8s/gcp`, values schema, Kustomize overlays, Workload Identity/IRSA, RBAC, NetworkPolicy and admission parity tests | Treat namespaces, SAs, selectors and policy subjects as authority-bearing identities. Reuse schemas/renderers; do not hand-edit generated overlays. |
| Releases/artifacts | Release Please root package, ADR-037 provenance workflows, exact OCI digests, package-native manifests and existing publisher configuration | Preserve the umbrella release; a coordinate migration is not a component-version redesign or deploy trigger. |
| CI/repository policy | `.ground-control.yaml`, `.github/quality-path-filters.yaml`, `_quality.yml`, `deploy.yml`, `scripts/quality_ownership`, `scripts/adr_guard`, actionlint, Dependabot and CodeQL | Update canonical identity and path routing together. Do not weaken fork, environment, ref, action-pin, secret or required-check gates. |
| Errors/logging/audit | `InstallationConfigError`, existing domain exception families, `shared.api.errors`, `shared.errors`, `shared.log_sanitize`, provisioner `log_redact`, ECS logging, `RequestIDMiddleware` and `shared.audit` | Reuse bounded envelopes, request correlation and sanitizers; no rename-specific exception hierarchy or raw provider/config output. |
| Observability | Existing metric namespace settings, provider adapters, alarms, dashboards and audit/provider logs | Preserve metric identity or migrate its readers/writers together. Keep historical continuity and distinct security principals. |

The standalone provisioner and Django platform intentionally have process-local
cloud exception and log-redaction modules. The rename is not grounds to add a
third shared framework or force one process to import the other.

## Cross-cutting layers the intended design must pass

| Layer | Required behavior |
| --- | --- |
| Repository authorization | GitHub repository name, immutable repository/owner IDs, protected ref, event, Environment, workflow and reusable-workflow claims all match the intended canonical repository. Branch protections and required checks are read back, not inferred from redirects. |
| Cloud workload identity | GCP WIF and AWS OIDC retain their existing role-specific claim contracts, including the profile distinction above. Kubernetes namespace/SA subjects continue to match IAM. No rename widens principal sets, role permissions, PassRole/actAs, secret access or destroy authority. |
| Runtime authentication | Session/API-token/CSRF, Firebase issuer/audience/tenant/revocation, email/MFA, OpenFGA and service authorization remain unchanged. User-visible issuer/TOTP labels may become BigRAE, but token issuer/audience values are protocol identity, not branding. |
| Root/config shape | Safe YAML duplicate/merge-key rejection, closed Pydantic models, backend settings models, MCP Zod/ADR-014 semantic checks, Terraform variable validation, Helm values schema, generated runtime inventory and per-setting validators remain authoritative. Old/new aliases converge before these checks or are validated by the same owner; conflicts fail before mutation. |
| Environment binding and worker admission | `config/_env_manifest.py` must discover aliases and helper-mediated reads so `generate_env_manifest` stays deterministic; installation `runtime_inventory*.py` and AWS/GCP renderers still own allowed, required, duplicate and secret key shapes. SDK input parsing, `shared.cloud.sensitive_env`, `shared.cloud.runtime_plugins` and both static/Helm runtime-plugin admission policies must agree on the exact invocation key and matching `secretKeyRef`. A new `_INPUT` alias is not sensitive under suffix rules alone. Admit one bounded Secret-backed input per invocation, never a literal payload, `envFrom`, or arbitrary prefixed key. |
| Secret surface | Config holds references, not payloads. Existing secret-store IDs may remain legacy physical identities. Any BigRAE reference alias resolves to the same authorized payload without duplicating it into source, workflow output, ConfigMap, logs, artifacts or summaries. |
| OS/process exposure | Repository names, config paths and non-secret resource IDs use structured argv or quoted environment/file inputs. Tokens, root-config secret payloads, OIDC claims, Terraform output objects and credential files never enter process argv or command traces; temporary sensitive files stay in `RUNNER_TEMP`/protected temp paths and are cleaned. |
| Schema/wire validation | Existing DRF, Pydantic/shared-schema, operation-envelope, RAES and package validators continue to own shape and version checks. Legacy discriminators remain accepted according to their existing windows; BigRAE names do not bypass digest, containment, size or closed-enum checks. |
| Persistence/state | Database rows, UUID derivations, replay/idempotency keys, outbox events, Terraform backends/addresses, cloud physical IDs, K8s identities and retained artifacts remain referentially stable. Every intentional physical move has upgrade, mixed-version and rollback evidence. |
| Error envelope | Installation failures remain value-free `ConfigIssue`/`InstallationConfigError`; settings use `ImproperlyConfigured`, MCP uses `PolicyError`, and guard checks use `Violation`. HTTP failures retain the shared `{error: ...}` envelope with request ID; provider/domain failures retain their owning exception type. Do not expose raw old/new config, claims, provider errors or state commands. |
| Logging/audit/metrics | Continue ECS/request-ID logging, strict mutation audit and existing sanitizers/fingerprints. Alias selection may log only the non-secret key class and deprecation state. Telemetry migration preserves alarm/dashboard coverage and does not silently split or double security signals. |
| Supply chain | Release, package, image and chart publication retain immutable source SHA/digest, SBOM/provenance, trusted publisher, signature/attestation and admission checks under the canonical repository. Old artifacts remain verifiable and usable for rollback. |
| Repository enforcement | Updated paths remain fully classified by quality ownership; workflows pass actionlint; Terraform, Helm/Kustomize and generated-contract checks run at their owning boundaries; the full ADR guard remains mandatory. |

## Evidence guardrails

No single fresh install, raw grep or green unit suite proves this cutover. Where
the ledger identifies supported consumers, evidence must cover
old supported config/CLI/package coordinates, exact old immutable artifacts,
new canonical coordinates, cross-version producer/consumer combinations,
rendered Kubernetes policy/identity parity, repository configuration readback,
future cloud-trust template validation, package/image provenance, OpenAPI and
backend-contract compatibility, and alarm/dashboard continuity. Evidence may identify synthetic
deployments and immutable public artifacts but must not expose credentials,
private pack data or internal-only operational material.

Reuse ADR-019 boundary-testing conventions: exercise real parsers, policy
evaluators and rendered admission rules with synthetic inputs. Packaging proof
uses installed wheels outside the checkout, including legacy distribution
requirements, extras, entry-point collisions and upgrade combinations within
the declared support window. Old/new import aliases must preserve class and
exception identity; copying model classes is not compatibility. A dual header
must sign identical bytes; accepting obsolete unsafe formats is not migration.
Source tests do not prove external publisher activation or downstream adoption.

Under the no-deployments premise, live state migration and workload rollback
are not applicable. If that premise changes, the state-preservation rules above
also require saved no-unintended-replacement plans, installed trust readback and
upgrade/rollback proof against the original state. Synthetic fixtures can test
these contracts without asserting that a deployment exists.

The eventual cutover and rollback runbook owns ordering and operator actions;
this note only fixes the architecture constraints. A rollback that requires
moving a tag, rewriting an attestation, restoring deleted state or recreating a
secret from logs is invalid.

## Extensibility seams

- Repository identity already has explicit `github_org`, `github_repo`,
  `github_repository_id` and `github_owner_id` parameters. A future approved
  ownership change must alter those reviewed inputs and trust tuples, not
  scatter another literal slug or add a second OIDC system.
- Deployment/resource naming already has `deployment.name`, Terraform
  `name_prefix`, Helm values/namespaces and explicit image roots. New installs
  may use BigRAE defaults while existing installs retain their resolved physical
  identity. Do not derive live resource identity from display branding.
- Published interfaces extend through their existing version seams: root/backend
  bundle contract versions, `/api/vN/`, wire discriminator versions, package
  versions and CLI aliases. One future rename/removal should require changing
  the owning compatibility table, not every caller.
- The reviewed identifier ledger is the rename-specific extension seam. A new
  `Shifter` occurrence must either be a declared compatibility/historical
  identity or fail the cutover inventory check. It is not a runtime naming
  registry and must not become application configuration.

## Gotchas and anti-patterns

- Do not run a case-insensitive global replacement, including over migrations,
  lockfiles, frozen schemas, changelog history, provenance or generated files.
- Do not rename Terraform labels and assume physical resources moved; do not
  rename physical resources and assume a `moved` block handles provider identity.
- Do not rename Kubernetes namespaces, ServiceAccounts or selector labels as
  branding. They are authorization, isolation and rollout inputs.
- Do not make BigRAE and Shifter config keys independent settings, accept
  last-one-wins behavior, or silently prefer one conflicting value.
- Do not publish a new Python/Node/Helm/OCI coordinate and remove the old one in
  the same release without consumer evidence. Import packages and distribution
  names are separate contracts.
- Do not reuse a schema/version discriminator with a different spelling or
  semantics. Mint a version only through the owning compatibility process.
- Do not change UUID namespace strings, idempotency keys, audit-chain material,
  object prefixes or persisted provider values for cosmetic consistency.
- Do not treat GitHub redirects as OIDC, reusable-workflow, trusted-publisher,
  attestation, Pages, badge, SonarCloud or runner compatibility.
- Do not weaken exact repository checks so both slugs “work”; any overlap is a
  bounded, ID-bound transition with explicit removal evidence.
- Do not copy secrets to new names as a source-code migration, pass them on the
  command line, dump environments/provider responses, or broaden secret prefix
  IAM to cover both names indefinitely.
- Do not rename metric namespaces without moving alarms, dashboards, queries,
  SLOs and retention expectations. Do not dual-publish indefinitely.
- Do not turn current product docs into historical text, or rewrite historical
  evidence as if it originally referred to BigRAE. Mark the distinction.
- Do not add a product-name service, branding DTO, rename exception hierarchy,
  second config loader, second deployment renderer, second release workflow or
  second observability stack.

## Non-goals and implementation boundaries

- No organization transfer, ownership change, new hosting, new registry,
  repository recreation or history rewrite.
- No application feature, authorization change, API redesign, schema redesign,
  tenancy change, OpenRAE ownership change or environment-pack distribution
  capability.
- No unrelated refactor of package layout, Django apps, service/repository
  boundaries, deployment orchestration, provider factories or logging.
- No requirement to cosmetically rename internal/physical identities whose
  retention is safer and explicitly recorded.
- No deletion of old artifacts, releases, packages, images, state, telemetry or
  compatibility coordinates before the ledger's milestone and evidence gate.
- No execution of the repository rename, cloud mutation, package publication,
  secret copy, trusted-publisher update or external ecosystem edit in this
  architecture-preflight change.

This preflight adds guidance only. It does not satisfy the issue's identifier
inventory, migration, cutover, rehearsal or external-coordination acceptance
criteria by itself.
