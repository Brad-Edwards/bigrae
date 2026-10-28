# Shifter-owned environment pack consolidation: architecture preflight (#2550)

> **Historical implementation record.** This issue-scoped preflight is not current-system authority. See the [current-state handbook](../technical/architecture.md) and accepted, superseding ADRs.


Status: guidance for implementation; no packs or runtime behavior changed here.
The supplied issue is the contract; there is no attached requirement.
This applies ADR-003, ADR-004, ADR-024, ADR-031, ADR-032, ADR-034 and ADR-053.

## Source layout and runtime references

Use `shifter/environment-packs/<existing-pack-name>/` as the canonical source
collection. Keep `preparation-http-smoke`, `shifter-raes-validation`,
`smoke-linux`, and `smoke-linux-aws` as independent complete pack directories.
Preserve their names, purpose, internal relative paths, bytes and Shifter
licensing. The collection is source organization, not a new distribution
catalog, Python package, pack format, or trust classification.

Keep the existing Docker context `./shifter`. The two smoke packs are explicitly
copied from the collection to their **existing runtime destinations**:

| Canonical source under `shifter/` | Image destination / persisted `package_ref` |
| --- | --- |
| `environment-packs/smoke-linux/` | `/app/cms/scenarios/inbox_packs/smoke-linux/` / `cms/scenarios/inbox_packs/smoke-linux` |
| `environment-packs/smoke-linux-aws/` | `/app/cms/scenarios/inbox_packs/smoke-linux-aws/` / `cms/scenarios/inbox_packs/smoke-linux-aws` |

Retain `cms/scenarios/inbox_packs/manifest.yaml` as the bootstrap authority and
retain `SHIPPED_INBOX_MANIFEST` as its trusted entrypoint. Its references name
runtime locations, not source-checkout locations. Explicit Docker copy rules
must agree with that manifest; verify the agreement rather than creating a
second packaging manifest or adding shipment flags to `pack.yaml`. Only the two
smoke packs ship and register automatically. Neither development pack is copied
to the platform image or added to bootstrap. Do not copy the whole collection
and delete unwanted packs later: their bytes would remain in image layers.

Build outputs and temporary test staging are projections of the single source,
not maintained copies. Remove the old pack source directories; retain the
bootstrap manifest and its documentation. No source symlink aliases, duplicate
fixtures, or resolver fallbacks are needed. Existing non-root image ownership
and permissions apply to the explicit copies.

`config/_raes_settings.py` already supplies `RAES_PACKAGE_ROOT` via
`SHIFTER_RAES_PACKAGE_ROOT`: checkout default is the repository root; deployed
default is `/app`. Development references become
`shifter/environment-packs/<name>` under the checkout root. Shipped manifest
tests must stage the real packs into an image-shaped root instead of assuming
that a persisted runtime reference is also the checkout source path. Keep those
two address spaces explicit in documentation and test helpers; do not add path
guessing or change the package-root default for every consumer.

## Durable identity and upgrade safety

Stable runtime destinations preserve existing `RaesPackageSource.package_ref`
values, release conformance references and rollback compatibility. A source-only
move needs no catalog revision, pack version bump, digest regeneration or data
migration. Verify unchanged digests through `raes-env-packs`, not a directory
hash. Do not edit inventory-covered pack docs merely to describe the collection;
put collection guidance outside pack roots. Keep SDL, associated artifacts,
provenance, delivery projections, and preparation inputs together.

If implementation instead changes runtime references, that is a durable catalog
revision, even with identical pack bytes. `cms.services.register_pack` compares
`package_ref`, version, digest, lock references and provenance for an idempotent
retry. Its existing explicit update requires `expected_package_digest` to match
the current row and a **different registration version**; changing the path and
leaving the version unchanged fails. Preserve the canonical digest when bytes
are unchanged. Do not mutate `pack.yaml` just to force a different digest.

Reuse `PackRegistrationRequest` and the service's locked replacement, scope
checks, strict audit and conformance reset. Bootstrap then re-establishes release
conformance through the normal path. No bulk ORM rewrite, migration-side service
clone, delete/recreate, digest-only retry equality, or automatic repair of
unexpected registrations. Its single predecessor-digest fence is not an
arbitrary historical-version upgrade mechanism; explicitly account for supported
source releases if reference revisions are chosen.

AWS bootstrap and the GCP migration Job run registration before rollout. Old
pods and rollback images do not know a newly introduced destination. Any design
changing references must resolve that mixed-version interval and rollback
contract before removing old paths; a passing fresh-install test is inadequate.
Preserving runtime destinations avoids this problem without retained source
copies. Manually registered development packs are not bootstrap-owned: document
their new checkout reference and the existing explicit update/conformance
commands where operators previously registered them. Do not add them to the
shipped manifest as a migration mechanism.

## Cross-cutting boundaries to retain

Paths below are relative to `shifter/shifter_platform/` unless qualified.

| Layer / incumbent | Required treatment |
| --- | --- |
| Authority: `shared.auth.validate_cms_authoring_user`, `cms.services.register_pack`, `cms/services/_content_authority.py` | Preserve operator/tenant scope checks and no-shadow behavior. Bootstrap uses its existing bounded non-login actor; source location grants no authority. Tenant session/CSRF and credential-scope behavior need no changes. |
| Manifest shape: `cms.scenarios.inbox.load_inbox_manifest` | Retain safe YAML loading, closed entry fields, slug/type/length checks and `PackRegistrationRequest`. No directory discovery, alternate parser, extra metadata schema or caller-asserted conformance. |
| Reference persistence: `shared.schemas.raes_package_source.PackageSourceRecord` / `validate_package_source`, `cms.models.RaesPackageSource.save` | Preserve source-kind/profile/digest shapes, bounded allowlisted reference-only provenance, ownership and current row identity. Store no SDL, payload, credentials or generated config in CMS records. |
| Pack validation and identity: `cms.scenarios.pack_validation` over pinned `raes-env-packs` | Preserve foreign-input validation, directory basename matching pack name, exact associated-artifact inventory and canonical byte verification. Do not add a local schema, digest algorithm or exemption for first-party sources. |
| Filesystem / parser: `shared.raes.package_loader.resolve_pack_root`, `resolve_pack_scenario_path`, CMS registration containment check | Keep resolution beneath the configured root, immutable staging, verification before SDL load and exactly one direct non-symlink SDL entry. Moving a parent directory grants no traversal/symlink exception or filesystem-order selection. |
| Conformance: `cms.scenarios.inbox._promote_release_conformance`, public `cms.services.validate_registered_pack_conformance`, `shared.raes.pack_conformance.validate_pack_contract` | Registration remains pending until trusted validation. Shipped promotion is restricted to the canonical manifest; validation compiles/plans without provisioning. Preserve atomic batch rollback. Conformance is not backend supply or live-range qualification. |
| Object/content delivery: `shared.raes.object_source`, `shared.raes.content_delivery_prep`, `shared.raes.pack_delivery_inputs`, `shared.cloud.types.ObjectStorage` | Preserve bounded immutable-object download, archive traversal/link/device rejection, cleanup, digest checks and content-addressed delivery bindings. Archive roots retain pack basenames. Relocation must not change projection-relative inputs or introduce a new store, downloader or guest storage credentials. |
| Config / host / secrets: `config/_raes_settings.py`, `config/env-manifest.json`, Dockerfile, `shifter/.dockerignore`, existing entrypoint and deploy renderers | Reuse the existing non-secret package-root setting; no new env bindings or config keys. Preserve excluded env files, non-root runtime, image provenance and credential handling. Only non-secret references belong in CLI argv; never pass credentials, pack bodies or environment maps as build args, command strings or logs. Do not execute pack-supplied code during image assembly. |
| Errors / observability: `cms.exceptions.CMSError`, `InboxManifestError`, `PackValidationError`, `RaesPackageError`, `shared.log_sanitize.safe_log_value`, `shared.audit`, `shared.api.errors.api_error_response` | Reuse existing exception translation, bounded diagnostics, sanitized API envelopes, request IDs and strict transactional audit. Missing packs, byte drift and identity conflicts remain failures; no catch-and-skip bootstrap, raw pack/secret logging or new exception hierarchy. |

Application callers continue through public service facades; shared loaders
remain independent of CMS and Engine internals. Adapter installation, feature
acquisition and backend image resolution retain their existing authorities.
Pack location must never become a provenance-based ingestion bypass.

## Repository-wide routing and evidence surfaces

- Docker consumers: `shifter/shifter_platform/Dockerfile`, `shifter/.dockerignore`,
  `_shifter-platform.yml`, `_gcp-dev.yml`, `scripts/bootstrap/gcp_control_plane.py`
  and `scripts/stack-smoke/stack_smoke.sh` all use the existing context. Avoid
  widening it to the repository root or changing unrelated image builds.
- Deploy classification: `.github/workflows/deploy.yml` must route the two
  shipped pack subtrees to portal-image/stack-smoke checks and the applicable
  GCP image/deploy path; adding only `portal_image` does not reach the current
  GCP caller condition. Development pack edits remain `quality_only`, with no
  image build, Terraform plan or deployment. Do not match the entire collection
  as either shipped or quality-only. Preserve trusted event/environment gates.
- Quality selection: `.github/quality-path-filters.yaml` and
  `scripts/quality_ownership/{contract,classify_paths}.py` are canonical. Merely
  invoking Quality is insufficient: route pack edits to existing platform pack
  tests, and validation SDL edits to provisioner OS tests as well. Avoid a
  config exclusion that classifies a path but selects no consumer tests. Check
  Markdown-only pack edits too: `quality_non_docs` excludes all `*.md`, although
  pack documentation can be digest-covered. Use existing guardrail routing to
  keep those changes quality-relevant.
- Enforcement: reconcile `_deploy_workflow_routing.py`,
  `_secret_hygiene_artifacts.py` and their tests under `scripts/adr_guard/`.
  They encode the old `scenario-dev/**` quality-only and
  `scenario-dev/<pack>/build/` generated-artifact boundaries. Keep equivalent
  protection at the new layout, including ignored/staged-file behavior; do not
  leave obsolete required globs or broaden exemptions. Review `.gitignore`,
  `.gitleaks.toml`, `sonar-project.properties` and `.github/codeql/codeql-config.yml`
  against actual new paths. Sonar currently excludes `scenario-dev/**`; CodeQL
  currently includes synthetic fixtures. Do not turn a move into a blanket
  scanner exclusion for all packs. Update ADR enforcement docs/registry with
  any changed checks; retain the no-Mission-Control-flag rule's domain boundary.
- Documentation: update the collection and bootstrap READMEs,
  `docs/ops/content-ingestion.md`, validation/image-registry guidance and current
  CI/ADR enforcement path documentation. Distinguish historical evidence and
  synthetic path examples from active runnable references; do not rewrite the
  generated changelog or historical evidence to pretend a different layout.
- Existing evidence owners: platform `tests/cms/test_inbox_bootstrap.py`,
  `test_content_ingestion.py`, `test_register_pack_command.py`,
  `test_pack_conformance.py`, `raes/test_pack_feature_artifacts.py`,
  `tests/config/test_full_cutover_1311.py`, shared RAES package-loader,
  object-source and backend-validation-package suites; provisioner
  `tests/test_raes_operating_system.py`; backend-validation command tests;
  quality-ownership/deploy-routing and built-image stack-smoke suites. Reuse
  their real contract/security checks. Evidence must cover unchanged canonical
  digests, manifest-to-image agreement, absence of development packs in the
  image/bootstrap, existing-row upgrade/retry, and both shipped/test-only
  routing. Runtime availability needs a built-image check, not source-only
  assertions. Live validation retains normal launch, redacted evidence and
  request-ID cleanup; unit conformance cannot stand in for it.

Use the existing Makefile/package-owned test policy for platform and provisioner
checks. Changes to workflow or guard code also require their existing tests,
`actionlint` for workflow edits, and
`python3 scripts/adr_guard/adr_guard.py --all --level ci`; Python/import edits
retain the repository's Ruff/import-linter checks. Do not create a second
verification script that restates package test policy.

The extensibility seams are the existing manifest entries, explicit Docker
copy selection and quality/deploy path classifiers. A fifth pack is an
independent directory; shipping it is a deliberate manifest/build decision.
Adding one must not require changing the loader, portable pack schema,
authorization or error contracts. No generic pack manager or build framework is
justified by these four directories.

## Non-goals and implementation boundary

This preflight changes guidance only. The subsequent issue is limited to source
layout, packaging, references and their tests/docs/routing. It does not change
scenario behavior, cloud addressing variants, software versions, licensing,
RAES semantics, backend qualification, tenant content lifecycle, adapter trust,
or private/external pack ownership. No repository transfer, new hosting,
publication service, catalog synchronization, application API or deployment
workflow fork is part of the consolidation.
