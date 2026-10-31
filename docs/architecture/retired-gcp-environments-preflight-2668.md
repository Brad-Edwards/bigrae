# GCP environment retirement and released guest images (#2668)

Architecture preflight, revised 2026-10-30. Implementation is pending. The
supplied issue is the contract; no requirement is attached. This note applies
[ADR-070](../adr/070-github-less-gcp-deployment.md) to environment retirement,
standalone developer image production, signed public guest artifacts,
release-pinned selection, and verified, idempotent import. It supersedes this
note's earlier removal-only guidance, which incorrectly deferred image
production. Related issues #2470, #2471, and #2672 do not reduce this issue's
acceptance criteria.

## Decisions and ownership

Remove the four deployment definitions and their active selection, routing,
defaults, and operational instructions. Keep shared release and GitHub-less
deployment capabilities. Deployment name, backend (`gcp`), profile (`dev`/`prod`),
GitHub Environment, build purpose, runner label, and scenario environment pack
are different concepts. Retirement does not authorize another assumed tenant.

`platform/charts/shifter/values-gcp-dev.yaml` is a generic development profile,
selected by `gcp_helm.profile_values_path()`, not a tenant definition. Keep its
contract and validation coverage. Historical prose, synthetic test literals,
Terraform `moved` blocks, and frozen published contracts are not deployments.
Acceptance is semantic removal, not a repository-wide string ban.

| Repository surface | Boundary to preserve or change |
| --- | --- |
| `platform/terraform/gcp/environments/{balrog,gcp-dev,orthanc,sauron}/terraform.tfvars` and `global/{cicd-oidc,github-runner}/{balrog,gcp-dev,orthanc,sauron}.tfvars` | Retire deployment and foundation/runner bindings. Keep reusable roots, modules, provider locks, and state-compatibility moves. |
| `.github/workflows/deploy.yml`, `_gcp-dev.yml`, `gcp-dev-destroy.yml`, `.github/actionlint.yaml` | Remove retired deploy/destroy inputs, routes, bindings, runner selections, and orphaned adapters. Preserve AWS routing and credential-free GCP quality checks. Delete `gcp-dev-destroy.yml`. |
| `packer-gcp.yml`, `packer-gcp-validate.yml`, `packer-gcp-promote.yml` | Build/validation select no tenant. Delete promotion; move its evidence guarantees to publication, not its dev-to-prod copy logic. |
| `scripts/bootstrap/{cli,bootstrap_core,preflight,gcp_runner,gcp_foundation}.py`, `scripts/gcp/render_ci_deployment_inputs.py` | Audit retired defaults and CI-only consumers. Preserve generic capabilities independently used by external-input deployment or image production; never substitute a new default tenant. |
| `shifter/engine/provisioner/{_gdc_vm_naming,gdc_vmseries_common}.py` | Retired-name fallbacks need the existing explicit runtime identity boundary. Preserve names for correctly configured deployments; do not rename live resources or retire GDC. |
| `scripts/adr_guard/_guard/_workflow_model.py`, workflow/security checks and tests, `platform/terraform/validation-inventory.yaml`, `scripts/check_tf_roots` | Retire obsolete caller expectations; retain semantic PR isolation, dispatch/ref gates, upstream failure, concurrency, quality routing, shared-root inventory and negative trust tests. Missing workflows must not become globally acceptable. |
| `installation/{gcp_release,bundle_gcp,runtime_inventory}.py`, `published_contract/`, `platform/deployer/gcp/Dockerfile` | Evolve the canonical release model and all its producers/consumers. Remove obsolete workflow ownership through bundle definitions and regenerate the current contract with `shifter-config contract export`; run `contract check`. Never hand-edit generated JSON or frozen v1 snapshots. |
| `.github/{quality-path-filters.yaml,workflows/_quality.yml}`, `.pre-commit-config.yaml`, `.trivyignore.yaml`, ADR registry/enforcement docs, `AGENTS.md`, `.gc/plan-rules.md` | Keep applicable coverage after deploy deletion. Review vulnerability exceptions against surviving consumers. Update obsolete ADR-003/004/037/070 caller descriptions with implementation, not blanket exceptions or weakened checks. |
| Bootstrap/Packer READMEs, deployment/secrets/runner handbooks, RB023, operator/release skills, requirement traceability | Current instructions and links must describe external tenant inputs and independent image production. Repair references to removed paths even in unrelated requirements; do not invent a requirement for this issue. |

## Independent image production

`gcp-foundation` must prepare the developer-supplied project for build,
validation, and export without a platform deployment. Reuse `gcp_foundation.py`,
`global/cicd-oidc`, `modules/cicd-oidc-identity`, and the optional
`modules/image-build-network`. The foundation already provides private VPC,
NAT, IAP firewalls, retained evidence, and separate build/validate identities.
It is not yet a complete standalone image foundation: export and Daisy scratch
storage, scoped bucket discovery/IAM, and access logging currently live in
`modules/packer-build-infra`, attached through platform-core. Factor/reuse those
capabilities at their Terraform ownership boundary. Do not copy resources into
workflow shell, require a platform apply, or duplicate network/storage modules.
Preserve existing state addresses with reviewed moves where ownership changes;
no live state migration is authorized by this preflight.

Explicit foundation inputs identify the developer repository, verified immutable
repository/owner IDs, project ID/number, region/subnet, and purpose identities.
Build-only contexts enable build/validate without deploy/destroy authority.
Outputs configure that developer repository's image jobs. No central tenant list,
borrowed tenant runner, `default` network fallback, or upstream repository
identity may stand in for this configuration. Keep separate purpose GitHub
Environments where needed for security: fixed build/validate contexts are not a
tenant Environment input. Removing all Environment bindings would break the
existing exact-subject WIF contract and purpose separation.

Reuse validation workflow/scripts under `shifter/packer/gcp/scripts/validate/`.
Publication depends on successful validation of the **exact numeric GCE image
ID**, protected build revision, and both first-boot/reboot health phases. Retain
no-SA/no-scope validation guests, IAP-only management, Shielded VM posture,
instance-local SSH keys, and independent read-only-disk SBOM collection with
pinned Syft. Packer success, candidate self-report, labels, or a family head do
not authorize publication. `verify-build-evidence.sh` and
`verify-promotion-evidence.sh` already bind source, candidate, run/attempt,
artifact identity, verdict, and raw-evidence/SBOM digests. Adapt/reuse this
verification at publication, removing its hard-coded `dev` assumption; do not
leave divergent copies after deleting promotion. Recheck candidate ID at export
to close image-name reuse races. Keep builder and validator capabilities separate;
a publisher consumes the validated export without platform or evidence-overwrite
authority.

Today `publish-gce-base-image` needs only `build`: it can publish an unvalidated
disk. Its replacement must fail closed on failed, skipped, cancelled, absent,
replayed, or mismatched validation. Across workflow runs, authenticate the exact
run and verdict artifact as promotion does; across jobs, bind explicit successful
dependencies and candidate outputs. Unchecked `workflow_run` payloads and artifact
display names are not authority. Retain private immutable evidence and raw guest
SBOMs; public outputs contain approved provenance and redacted verdicts only.

Publish the existing GCE-native `disk.raw` tarball OCI contract, not a container
image or new archive format. Reuse the SHA-pinned
`actions/attest-build-provenance` pattern from `release-gcp.yml`, signing the
exact **OCI artifact manifest digest** and storing its Sigstore bundle as an
OCI referrer. An annotation is not a signature. Prove generic-artifact
attestation interoperability with `gcp_oci`/`gcp_provenance`; a successful push
alone is insufficient. No candidate disk publishes before validation passes;
an incomplete push/attestation must never become a selectable release guest.
Packages and referrers must actually permit anonymous public reads. A developer
token publishes into its authorized namespace; a fork has no upstream package
rights.

The shared Sigstore policy currently checks repository/owner/revision and digest,
not a validation verdict or publisher workflow identity. Do not present that
signature alone as proof of guest health: protected publication and its verified
evidence supply that guarantee. Any additional signer-workflow restriction
belongs in this same provenance policy, not an independent signature verifier.

## Release selection and import

Extend the canonical closed model in `installation/gcp_release.py` and its
`shifter-config gcp-release-manifest` producer. A release records a complete
role-keyed guest selection with exact OCI references and each guest's build
revision, alongside existing application images. Guests can predate the
application revision. Reuse this model in release workflow, deployer build,
bootstrap CLI and tests; no parallel guest manifest/parser in shell or Terraform.
Evolve the schema version explicitly: current v1 is closed, contains only five
application images, and cannot promise release-bound guests. An old document
without guest pins must fail clearly for this import, never fall back to latest
images. Old releases retain their own tooling; do not rewrite old manifests.

Guest selection is an explicit reviewed release input, not newest tag/family
discovery during release. An application release consumes that selection; it
neither dispatches Packer nor exports, rebuilds, re-pushes, or re-attests unchanged
guests as though built at the application revision. Rebuild is an explicit
developer image operation. Preserve `release-gcp.yml`/`release-please.yml`
protected-ref/caller gates, separate `gcp-release` Environment, GitHub-hosted
runners, cloud-free publication, scans, SBOM/provenance, and ADR-069 identity.
`dev` and `deploy-test` release branches are not retired tenants. Keep the
five-workload inventory and tooling-to-release revision check. Disk artifacts do
not enter container scan, `DEPLOYMENT_ROOT_KEYS`, rollout or running-container
identity loops.

Reuse `gcp_provenance.ReleaseTrust` and `verify_image_provenance` at each guest's
recorded build revision: GitHub issuer, independently configured immutable
repository/owner IDs, Sigstore verification, SLSA v1 predicate, exact digest
subject. Default to `settings.release_trust`, including a developer publishing
its own releases. Never derive trusted signer IDs from annotations, registry
owner, or an unverified manifest. Accepting another build repository requires
explicit policy through the existing installation-config boundary; a valid
signature from any repository is insufficient. Do not invent a multi-builder
registry for the single developer-repository use case. Verify guests at release
selection admission and again before tenant import.

`gcp-images` consumes the release through the existing manifest-loading boundary,
including the deployer-embedded default. `--source`/mirror configuration changes
transport location only, retaining selected digests and referrers; it no longer
selects versions. Reuse `gcp_base_images.ResolvedArtifact`, OCI artifact/layer/
role checks, import/readiness/cleanup and runtime projection. Resolve and
cryptographically verify the **whole set before any mutation**, including reuse
paths. Fetch only the pinned disk layer and verify bytes against its descriptor;
never unpack an untrusted archive locally to discover its filename.

Transfer remains in the explicitly selected range project/region, which can
differ from the control-plane project. Keep per-invocation buckets with uniform
access, public-access prevention and zero soft-delete duration; pass explicit
project flags. Adopt ownership only after creation succeeds, and remove exact
owned objects/buckets on success and failure. Cleanup failure fails the command;
do not delete durable images or retained evidence as transfer cleanup.

Keep deterministic digest-derived GCE naming, full source/digest/role ownership
checks, `READY`, `UEFI_COMPATIBLE`, and DC `WINDOWS` feature checks. Matching
compatible images skip disk download, upload, and image creation; an all-reused
set need not create the transfer bucket either. Short digests are names, not
identity checks. A mirror must not make an unchanged canonical release source
look like a new image: current full-package comparison needs an explicit
canonical-source versus transport distinction. Never overwrite/delete a foreign
name collision. Retain safe failed/incompatible owned-image handling and report
partial completion for retry. Commit `range-images.json` only after all roles
are ready; valid durable imports survive later failures.

The canonical role seam is `BASE_IMAGE_ROLES` and existing runtime mappings:
public `ubuntu` becomes `linux` in `range-images.json`; `dc-prebaked` produces
public `dc`. Generalized Windows and first-boot DC lack matching validators;
selectable build types do not automatically become publishable. A future role
needs a validator, guest-feature contract and one declared mapping, not another
tenant selector. Public disks contain redistributable generic content only:
no private seed, credentials, transcripts, pack identity or answers. Reuse Packer
cleanup/finalize controls. Boot health and SBOMs alone do not establish
sanitization or Windows redistribution/licensing compatibility.

## Cross-cutting layers the design must pass

| Layer and canonical incumbent | Required property |
| --- | --- |
| Config parsing: `installation.loader.load_root_config`, `schema.RootConfig`, registry, `settings_gcp.py`, `settings_gcp_deploy.py`, `gcp_inputs.load_deployment_inputs` | Preserve duplicate-key rejection in the root loader, closed backend/profile models and policy validators; preserve bundle allowlist, size bounds, JSON/image-reference checks and deployment-name grammar. Foundation/release JSON currently use plain `json.loads`: closed-model validation alone does not reject duplicate keys. Apply repository duplicate-key/size/object-shape conventions at the owning parser, including foundation purpose-context shapes before iteration or commands. Release changes go through its canonical parser/CLI; reject malformed, unknown or incomplete guest roles and digest/revision shapes. No parallel DTO/parser/validator. |
| Terraform shapes and host execution: `gcp_terraform.render_var_files`, `installation.render.render_tfvars`, `validate_gcp_control_plane_security_inputs`, root/module validation | Keep operator values, typed projection, command-owned values precedence. Foundation build inputs must pass project/name, repository ID, subject-format, exact Environment/ref/workflow tuple and disjoint-purpose validation. Verify supplied project/repository IDs before cloud mutations. Keep backendless locked validation, private `TF_DATA_DIR`, saved-plan apply, provider locks, state prefixes, and independent foundation state. |
| Auth/policy: `gcp_foundation`, `gcp_identity.PurposeIdentities`, identity Terraform, `check_tf_gcp_wif_trust` including `_resolved_plan`, `check_tf_gcp_iam_resource_scope`, `check_tf_gcp_runner_network` | Preserve workflow pre-auth ref gates plus provider-level exact protected refs/workflows, immutable anchors, exact SA subjects and capability separation. No wildcard principalSet, shared cross-purpose subject, key, project-wide Storage Admin or default identity fallback. Build caller/builder/Cloud Build/export use the one existing build identity with self-scoped grants. Foundation currently applies a saved plan without calling `_resolved_plan.check_resolved_plan`; trust-changing foundation work must reuse that existing comparison against authorized inputs before apply, rather than claim tests are a runtime gate. GitHub-less tenant mode retains empty federation and named operator impersonation. Cloud authority does not grant application catalog authority. |
| Release/registry trust: `gcp_deploy.load_manifest`/`release_trust`, `gcp_provenance`, `gcp_oci`, `gcp_release_images.docker_config` | Reuse `ImageReference.parse`, bounded OCI metadata reads, digest verification, Sigstore policy and referrer fallback. Preserve app gates; guest verification uses guest build revisions. Only `is_artifact_registry_host` hosts get Google credentials; replace the legacy CLI's broad `.pkg.dev` match by reusing the strict helper. Public pulls use isolated Docker config without inherited unrelated credentials. Keep large disk transfer out of the 4 MiB metadata reader. |
| Runtime/env bindings: `gcp_inputs.parse_range_images`/runtime projection, `gcp_runtime.compose_runtime_env`, `gcp_control_plane.parse_env_contract`, `scripts/gcp/render_runtime_env.py`, `installation.runtime_inventory*` | Keep native exact GCE references, existing role/env keys and backend policy; release-driven output uses exact images even though the generic parser also accepts families. Preserve Helm schema, runtime/chart contract tests, admission, RBAC, NetworkPolicy, and native identity/model-broker validation. Do not expose OCI disk refs as container/env secret bindings. |
| Secrets and OS boundary: `bootstrap_core.run_cmd`/argv checks and `run_cmd_secret_stdin`, `gcp_inputs.write_private_file`, `gcp_process.ProcessRunner`, `isolated_env`/`scoped_environment` | Use fixed argv lists without shell evaluation; Packer secrets use `PKR_VAR_*`, registry login uses stdin. Keep 0700 directories/0600 files, temporary credentials and cleanup. Secrets stay out of argv, evidence, Helm history, fixtures and logs; redacted logging does not prevent process-list exposure. Use `sensitive=True` for secret-reading subprocesses or the incumbent secret-stdin helper where appropriate. Treat workflow inputs as data via `env`; validate before `GITHUB_ENV`/`GITHUB_OUTPUT` writes. Reject newlines, option-like names, traversal and unchecked profile/script paths; generate JSON structurally. |
| Errors/observability: `installation.errors.ConfigIssue`/`InstallationConfigError`, `DeploymentInputError`, `BaseImageError`, `ProvenanceError`, `GcpCommandError`, CLI dispatch, `gcp_evidence` | Preserve value-free config diagnostics and bounded/sanitized domain errors, nonzero failures, run/attempt/candidate/digest bindings, private evidence, redacted verdicts and cleanup failure visibility. Existing legacy ORAS errors can echo raw stderr: do not copy that leakage into provenance/config errors. Never log tokens or raw private evidence. No new exception family, logger or public artifact sink. Advisory application smoke is not the guest publication gate. |
| Persistence/concurrency: `gcp_lock`, `gcp_config_store`, `gcp_terraform`, evidence bucket IAM/retention | Image import is currently outside the lifecycle lock and `write_range_images` merges without compare-and-swap. Serialize import/config publication with the existing generation-precondition lock or explicit caller serialization, including deploy/config writers. Per-run buckets do not prevent deterministic image-name races or lost updates. Queue competing workflow publications without cancelling cloud mutation in flight. Retain private objectCreator/retention evidence boundaries and narrowly owned transfer cleanup. No new database or image ledger. |

The extension seams are validated external tenant inputs, separate developer
foundation inputs, role-keyed release guest selection, per-artifact build
revision, configured signer trust and a transport-only mirror prefix. A new
tenant, developer repository/project or mirror must work without adding its
identity to canonical code/workflows. Registry namespace and signer authority
are separate; neither a fork nor a mirror changes trust.

## Verification boundaries and non-goals

Reuse bootstrap base-image/release-gate/foundation/input/process tests,
installation release/contract/runtime tests, Packer evidence/tunnel tests,
workflow-model/security tests and Terraform native trust tests. Preserve
negative tests while replacing obsolete tenant fixtures with synthetic inputs.
Required evidence includes standalone foundation with no platform resources;
validation failure blocking publication; actual guest-OCI signature/referrer
interoperability; role/digest/revision/signer/evidence mismatches rejected before
mutation; different app/guest revisions; unchanged guest selection across
releases; anonymous and mirror pulls; second import creating no image; and
partial failure, cleanup, collision and concurrent image/config behavior.
Static workflow tests do not prove guest boot, licensing or public visibility.

Run the full ADR CI guard. Workflow changes require `actionlint`; Terraform
changes require inventory, formatting/locked backendless validation, TFLint and
existing blocking Checkov/GCP policy checks. Relevant chart/runtime changes need
supported renders, kube-linter, pinned kubeconform and Kubernetes Checkov gates.
Keep quality routing after deploy removal. Update ADR-037-R8/R9 and ADR-070-R1/
R5/R8 wording and evidence with implementation: current rules still describe
separate promotion and prefix discovery. Do not mark new controls enforced
before their live checks exist; this preflight adds no enforcement tool.

Implementation also removes retired GitHub Environments and `orthanc`/`sauron`
branches from `.ground-control.yaml`'s canonical repository, with scoped readback
evidence. Audit associated purpose Environments while retaining unrelated
`gcp-release` and developer build/validate contexts. Deleting YAML does not
delete GitHub resources; stale workflows on retained refs can recreate them.
Branch deletion remains subject to the repository's synchronous approval rule.
This preflight request authorizes no resource deletion. The issue states the
retired deployments do not exist; repository/metadata removal needs no cloud
teardown.

Non-goals here: implementation, cloud/GitHub mutations, publication and an
implementation plan. Implementation boundaries: no tenant teardown/migration,
AWS/GDC retirement, application API/controller/DTO/model/database changes, new
exception/logging families, generic image framework, private-package credential
service, private pack transfer, or generated changelog/frozen-contract rewriting.
Independent guest production and canonical release-schema evolution are in
scope, not deferred follow-ups.
