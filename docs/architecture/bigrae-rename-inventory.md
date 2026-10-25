# BigRAE identifier inventory

Issue: #2555. This ledger owns the naming dispositions. Internal source layout
and deployed identity are independent of product branding. Each family below
includes generated copies and its compatibility fixtures, unless stated otherwise.

Binding user decision, 2026-10-25: **assume no deployments exist**. References
below to resource, protocol or artifact identity classify source contracts;
they do not assert deployed infrastructure or live consumers. No foundation
input, cloud apply, deployed-state migration or live-workload rollback is needed.

`rename-now` is current vocabulary, `compatibility` is a supported old input,
and `permanent` is an intentionally retained identity. Compatibility removal is
gated on the first major release after the BigRAE adoption milestone, downstream
migration evidence, and a reviewed removal issue. No alias expires merely because
the milestone closes.

| Family / owning source | Consumers | Disposition | State and security impact | Verification / removal evidence |
| --- | --- | --- | --- | --- |
| Product text, templates, SPA, favicon, mail: `shifter/shifter_platform/templates`, `frontend`, `static/images` | Browser, participant and operator | rename-now: BigRAE | No authority or resource identity change | Rendered UI tests; frontend build; translation generation |
| Admin labels, OpenAPI title: `cms/apps.py`, `engine/apps.py`, `config/_drf_settings.py` | Admin and API documentation | rename-now | API paths, operation IDs and DTOs unchanged | API contract generation and compatibility check |
| TOTP issuer and display label: `config/_oidc_settings.py`, `config/identity_platform.py` | New authenticator enrolments | rename-now | Existing enrolled credentials and Firebase/OIDC token issuer validation are retained | Identity Platform/settings tests; existing credential authentication |
| VPN download name: `mission_control/api/_vpn.py` | Browser download | rename-now: `bigrae-range.ovpn` | Profile bytes and certificate identities unchanged | VPN API tests |
| Current product prose: root Markdown and `docs/{technical,features,getting-started,how-to,dev,ops,qa}` | Users, operators, maintainers | rename-now | Inline source/resource/wire coordinates retain their independent disposition | Documentation build and link checks |
| Root CLI: `installation/pyproject.toml`, `cli_parser.py`, `cli.py`, `scaffold.py` | Operators and bootstrap | compatibility: `bigrae-config`, `bigrae.yaml`; legacy command and explicit paths work | One loader validates both. Conflicting default files fail with value-free errors. Explicit paths select one config | Installation CLI/scaffold/alias tests; remove only after operator migration evidence |
| Runtime `SHIFTER_*` variables: `config/_product_env.py`, owning settings modules, generated `config/env-manifest.json` | Django settings | compatibility: `BIGRAE_*` aliases | Equal aliases converge; differing values fail before use. Existing type/range checks remain authoritative. Logs name keys only | Product env, settings, manifest and runtime inventory tests |
| Bootstrap `SHIFTER_CONFIG*` secret names and `.shifter.yaml` MCP policy | Existing workflow/bootstrap/MCP consumers | compatibility | Separate configuration schemas. Retain secret payloads and policy owner; no copying payloads or relaxing policy | Bootstrap preflight and MCP policy tests; coordinated operator migration before removal |
| Adapter distribution/imports: `shifter/shifter_adapter_sdk/pyproject.toml`, `bigrae_adapter_sdk` | External adapters | compatibility: `bigrae-adapter-sdk` / `bigrae_adapter_sdk`; legacy import package shipped in same wheel | Re-exports preserve class identity; executable plugin loading remains isolated | Build/install real wheel outside checkout; identity and isolation tests; legacy distribution deprecation release before publication cutover |
| Client distribution/imports: `shifter/shifter_client/pyproject.toml`, `bigrae_client` | Native API clients | compatibility: `bigrae-client` / `bigrae_client`; legacy import shipped | Same ADC/impersonation, audience and redirect checks | Installed-wheel import/transport tests; legacy distribution deprecation release before cutover |
| Runtime plugin groups, `shifter.runtime-plugin/v1`, `SHIFTER_PLUGIN_INPUT`: SDK and `shared/cloud/runtime_plugins.py` | Isolated adapter workers | compatibility: `bigrae.*`, `bigrae.runtime-plugin/v1`, `BIGRAE_PLUGIN_INPUT` | Protocol choice is bound to invocation and digest; conflicting environment aliases fail without outputting values; sensitive input remains Secret-backed | SDK worker/replay tests; cloud Secret injection tests; old worker receives legacy key |
| Verification entry-point group: SDK verification discovery | External verification plugins | compatibility: accept `bigrae.scenario_verification.adapters` and legacy group | Metadata-only discovery; ambiguous selections fail before load | Discovery and packaging tests; external plugin migration before removal |
| `X-Shifter-Signature`: `ctf/services/webhook.py` | Existing webhook receivers | compatibility: send identical `X-BigRAE-Signature` | One HMAC over identical bytes; egress validation and revocation checks retained | Signature equality and pinned-egress tests; receiver migration before removal |
| `shifter-challenges/v3`, earlier versions, CTF `fmt=shifter` | CTF export/import | compatibility through existing version gate | Legacy v1/v2 remain rejected because they contain obsolete flag representation. Branding cannot make unsafe formats importable | Transfer round-trip and rejection tests; any new discriminator needs versioned migration |
| `x-shifter:*`, RAES backend/profile `shifter`, realization envelopes and digests | Pinned RAES contracts and environment packs | permanent until upstream alias contract is qualified | Exact artifact/capability claims, content digests and backend identity must remain consistent | RAES manifest, realizability and conformance tests; ecosystem coordination |
| `shifter/`, Python modules/classes, JavaScript identifiers, CSS classes, Django app labels and migration dependencies | Internal code, tooling, database | permanent | Renaming would change imports, tables, content types or migration graph | Import boundaries and existing tests |
| UUID seeds, `shf_` tokens, signing salts, browser storage keys, media types, wire discriminators, operation envelopes, schema IDs | Issued credentials and persisted/replayed records | permanent | Identity, signature, replay and referential continuity | Existing authentication, replay and contract tests |
| Cloud names/tags, Terraform addresses/backends, storage/object prefixes, DB/role names, Kubernetes namespaces/SAs/selectors, Helm name/labels | Deployment templates | permanent | Retained internal source coordinates; no deployed state is asserted | Native template and rendered policy validation; live migration is not applicable |
| `Shifter/*` metrics, log namespaces and alarm/dashboards | Monitoring and audit consumers | permanent | Preserve time series, alarms, security signals and retention continuity | Existing observability tests and readback |
| Existing GHCR package/image roots, OCI digests, pinned image-family names, signed releases and attestations | Deployment and rollback | permanent | Historical provenance remains bound to the original repository; never retag or rewrite it | Verify immutable old digests with original identity; qualify new coordinates separately |
| Helm description, schema descriptions, umbrella release package name | New documentation and releases | rename-now | Chart resource names/selectors and umbrella version unchanged | Helm schema/render and Release Please config checks |
| GitHub slug, OIDC/WIF template names, attestation expected repo, Pages URL, canonical tooling | GitHub, CI, provenance, docs and future deployments | rename-now in ordered repository cutover | Repository ID remains `1241599684`; current owner ID remains `56809295`. Names, IDs, protected refs, environments and workflow claims must agree | GitHub identity/settings readback, CI, native template validation and source/repository rollback evidence |
| ADR bodies, preflight records, requirements, deprecated docs, changelog, frozen snapshots and issue URLs | Historical evidence | permanent | Preserve original decisions and evidence. Current guardrail changes receive a separate recorded amendment | ADR guard, history and compatibility checks |

## Acceptance evidence

Source tests prove compatibility of code and local build candidates. The
repository rename additionally requires GitHub identity/settings readback,
hosted CI verification and ecosystem coordination. Under the no-deployments
decision, deployment-state migration and live-workload rehearsal are not
applicable; no cloud configuration file is required to establish that premise.

### Source preparation, 2026-10-23

The working change on `2555-bigrae-rename`, based on `a80fbc061`, contains the
product vocabulary and compatibility implementation. Local checks cover real
wheel installation of new and deprecated distributions, legacy/new adapter
groups and protocols, config conflict rejection, webhook signatures, CTF format
round trips, Secret-backed plugin admission, repository identity guards,
OpenAPI compatibility, frontend tests/build and native infrastructure checks.

The earlier foundation-input blocker was based on an unsupported deployment
assumption and is superseded by the user's
2026-10-25 no-deployments decision. The required full ADR check separately
reported five expired exceptions and their quality-ownership gaps. The user
expanded #2555 on 2026-10-25 to repair those failures under GEN-002.
The [quality design](bigrae-rename-quality-design-2555.md) defines the CI
boundaries and coverage for that scope.

The full CI-level ADR check passes. The guard tests also pass at the initial
repair checkpoint. Integration changes receive targeted checks before publish.
Blocking native checks own each remaining production path.
The five expired exceptions have been removed without renewal.
The initial repair reduced the bootstrap internal-mock allowance from 256 to 204.
The integration merge retains the newer reduction to 175.
The user approved removal of the two unused CloudFormation installers.
Their identifier exception and quality owner have been removed.
The unused root branding assets have also been removed after a consumer search.
MCP audits, lint and tests pass, including negative security probes.
Guacd runtime checks and a real PostgreSQL initialization-failure test pass.
The review's config-option and guide mismatches are repaired.
The export API helper accepts BigRAE and both supported legacy formats.
Config-sync regression tests and the frontend production build pass.
No release, readiness or completion is claimed from source checks alone.

### Repository cutover, 2026-10-25

The same-owner rename to `Brad-Edwards/bigrae` is applied. GitHub returned the
same repository ID, `1241599684`, and owner ID, `56809295`.
PR #2560 targets `dev`. Current source, repository tooling, new image source
labels, attestation checks and future-deployment examples use the new slug.
The Sonar gate accepts only the new slug with both immutable IDs.
The Ground Control project key and Sonar project key retain their existing
independent identities. Historical issue links, published artifact provenance
and pinned pack source records retain their original coordinates.

GitHub readback confirms unchanged branch protections for `dev` and `main`,
rulesets, Environments, Actions permissions and hooks. The four runner records
and 22 repository secret names remain present. The existing Sonar project key
and organization variables remain set. Old repository, issue, release and Git
URLs resolve to the renamed repository. Pages reports the new `/bigrae/` URL,
which returns HTTP 200; the old `/shifter/` site returns HTTP 404. Current site
links use the new path. No second hosting destination has been created.

Hosted checks must pass on the final PR head before delivery readiness.
The cutover does not publish packages or transfer the repository to another owner.
