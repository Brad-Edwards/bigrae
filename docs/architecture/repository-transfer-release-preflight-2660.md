# Repository transfer and release publication preflight (#2660)

This is design guidance, not an implementation plan or evidence of an operator
cutover. The issue's supplied outcome and acceptance criteria are the contract;
#2662 remains a delivery dependency. No formal requirement is attached.
Existing uncommitted workflow and documentation edits were inspected as proposed
behavior, not treated as proof that GitHub or deployed configuration is correct.

## Boundaries and decisions

Reuse ADR-069's four distinct identity sources: runtime context for self-identity,
`docs/adr/repository-identity.json` for upstream-only gates, deployment configuration
for trust, and an explicit target or the clone's `gh` default for operator tooling.
`.ground-control.yaml` is the canonical repository for issue and PR operations.
A transfer preserves the repository ID but changes the owner ID. A repository
redirect, registry path, or successful pull is not evidence of trusted provenance.
Do not add old/new-owner fallback trust or infer tenant trust from an artifact.

Keep release-please responsible for versions, tags, and GitHub Releases. Reuse
`release-gcp.yml` as the single GCP publisher through a local `workflow_call` from
`release-please.yml`, with the released version and commit passed explicitly.
The pre-push review moved the gate from `release_created == 'true'` to a release
tag that points at the pushed commit, so a re-run of a lost run still publishes. Retain manual dispatch for the existing
protected release refs (`main`, `dev`, `deploy-test`). Do not introduce a second
build pipeline, PAT-driven release event, tag deployment, or cloud release role.
The current workflow comments already explain why a release event produced with
`GITHUB_TOKEN` is insufficient for chaining.

Amend the existing ADR-004-R23 rule through ADR-070 and its registry/enforcement
documentation; reconcile ADR-042-R2's publication boundary too. A signed GCP
publication is allowed; deployment remains a separate operation. No new ADR,
release schema, persistence service, or application abstraction is needed.

## Cross-cutting layers the design must pass

| Layer and canonical incumbent | Required behavior |
| --- | --- |
| Repository identity parser: `_guard/_repository_identity.py`; identity check and tests under `scripts/adr_guard/` | Keep the declaration well formed, owner consistent with the slug, `.ground-control.yaml` aligned, and Sonar IDs equal to the declaration. Use synthetic identities in fixtures. Do not weaken literal detection or replace ID gates with names. |
| Workflow policy: `_workflow_model.py`, `checks/release_workflow.py`, `tests/test_release_workflow.py` | Reuse the YAML loader, expression helpers, `Violation` records, and existing caller/ref/action/provenance checks. Only the intended main release caller may call the publisher; no inherited secrets. Check both workflows and any newly introduced callers. Static guard success must not substitute for runtime checks. |
| Authorization: `gcp-release` Environment, workflow permissions, first ref check | Every publishing job stays on GitHub-hosted runners behind the Environment and protected ref allowlist before registry authentication. Caller and callee grant only contents-read, packages-write, attestations-write, and id-token-write as needed. Release-please's contents/PR write permissions remain in its own job. Read back Environment branch/reviewer policy and organization Actions restrictions after transfer. No GCP credential is needed for publication. |
| Input and environment-file boundary: `release-gcp.yml` and `shifter/installation/gcp_release.py` | Require a nonempty full source SHA on automatic calls and equality with the actual checked-out/build SHA. Bind the version to the release-please output. Validate version text using the existing model constraints **before** writing `GITHUB_ENV`, emitting commands, or publishing. Environment-file injection is possible even when the shell variable is quoted. Keep Actions expressions in step `env` or structured action inputs, not interpolated shell source. |
| Release/config shape: `GcpReleaseManifest`, `shifter-config gcp-release-manifest`, `GcpReleaseTrustSettings` in `settings_gcp_deploy.py` | Preserve closed models, the full revision, chart version, and all five exact image identities. Reuse the CLI/parser; do not hand-write a parallel JSON schema or relax unknown-field/digest checks. Required workflow strings alone do not validate content. |
| Build boundary: `platform/deployer/gcp/Dockerfile` and its `Dockerfile.dockerignore`; existing five image builds | Deployer manifest and embedded tooling must name the same source revision. Preserve frozen dependencies, checksum/digest pins, SBOM, maximum provenance, and exact-digest attestations. Preserve context exclusions for deployment inputs, local state, credentials, and caches; build arguments and public provenance may contain only public build metadata. |
| Signature/registry boundary: `scripts/bootstrap/gcp_provenance.py`, `gcp_oci.py`, `gcp_deploy.release_trust` | Keep OIDC issuer, configured repository/owner IDs, source revision, SLSA predicate, and exact subject digest checks. Verify registry referrers without GitHub access. A conflicting CI override fails. Retain anonymous public-registry reads and the restriction that Google credentials go only to Artifact Registry hosts. Never skip verification to accommodate the new owner. |
| Host processes and secret handling: `gcp_process.ProcessRunner`, `bootstrap_core` logging, existing login/scan actions | Use fixed argv and explicit child environments for operator subprocesses; credentials travel through supported environment/stdin/action secret inputs, never argv, Docker build args, logs, or committed files. Retain redacted argv, sensitive-output suppression, timeouts, and cleanup. Never inspect secret payloads or the user's secret file. |
| Error envelopes: `ConfigIssue` / `InstallationConfigError`, `RepositoryIdentityError`, `ProvenanceError`, `GcpDeployError`, `gcp_cli.dispatch_lifecycle` | Reuse existing field-path diagnostics, bounded credential-safe failures, and nonzero CLI exits. Do not add an exception hierarchy or dump rejected config, raw validation objects, registry headers, or token responses. No Django/API error-envelope change is involved. |
| Scan/evidence: `release-gcp.yml`, `gcp_release_scan.py`, `gcp_evidence.py`, `_quality.yml` | Keep the deployer digest's blocking scan before its attestation and retain control-plane scans at the existing deployment gate. Preserve pinned tools, reviewed expiring exceptions, Sonar's blocking quality gate, and private raw scan evidence with redacted verdicts. Public summaries identify version, source SHA, exact deployer digest, and run/attempt without tenant details or vulnerability payloads. |

## Transfer surface and acceptance traps

- **Security reporting:** change the existing policy and report-form link;
  enable and read back GitHub private vulnerability reporting separately. A
  policy edit is not a settings change. Keep actual vulnerability reports out
  of public issues and CI logs. No new email parser or reporting application.
- **Sonar:** `.ground-control.yaml` still names the former Sonar organization
  and project key in the inspected tree. Establish the actual destination
  project binding rather than guessing names from the new GitHub owner.
  Align non-secret `SONAR_PROJECT_KEY` / `SONAR_ORGANIZATION` repository
  variables and Ground Control configuration; retain `SONAR_TOKEN` in the
  secret environment, the ID/fork-origin policy, and `qualitygate.wait=true`.
  Confirm the app is installed for the destination organization and the scan
  actually runs. A skipped scan or successful upload is not a passing gate.
- **Images:** the existing lowercased runtime owner/repository seam supplies
  the new GHCR namespace. Existing packages do not become new-owner packages
  through repository redirects. Verify publish access, package linkage, and
  anonymous access to all five images, the deployer, and their attestations.
  New packages can be private; a successful authenticated workflow does not
  satisfy public release acceptance. Do not move old tags or rewrite old
  signatures. Publication and package visibility require operator authority.
- **Cloud trust:** inspect both GCP WIF and AWS OIDC configuration surfaces,
  not only the issue's table. Existing `platform/terraform/global/iam/*.tfvars`
  and `platform/terraform/gcp/global/cicd-oidc/*.tfvars` are deployment-owned
  inputs; generic modules are not their migration mechanism. The GCP WIF module
  and root retain a narrow repository-name default for CKV_GCP_125 static
  checking (ADR-069); evaluate that scanner default separately from real trust.
  Reuse `scripts/check_tf_gcp_wif_trust` and resolved-plan/installed-policy
  verification where federation exists. Never add default immutable IDs.
  GitHub-less tenants instead require the explicit owner change in each
  `settings.release_trust`; there is no automatic tenant migration. The older
  rename note's no-deployments premise is not proof of current deployment state.
  If no deployments exist, record that evidence rather than provisioning any.
- **Repository integrations:** read back actual repository/owner IDs,
  protections, required checks, Environments, Actions permissions, app/webhook
  access, runner bindings, and variable/secret *names*. Update clone defaults
  and remotes deliberately. Audit `.github/CODEOWNERS` for destination access;
  do not rename valid individual owners just because the repository moved.
  Retain historical links and retained compatibility coordinates rather than
  globally replacing every old-owner string.
- **Pages:** `mkdocs.yml` is the canonical site/repository URL configuration.
  Source redirects do not prove Pages works. Check destination Pages settings,
  generated base paths, and the existing documentation publishing workflow.
  Do not create a redirect repository or another hosting service.

## Reliability, extensibility, and verification boundary

The automatic path must prove that release version, tagged commit, actual build,
manifest, embedded tooling, and signatures agree. The inspected callee compares
source SHA only when nonempty; the caller guard checks SHA wiring but does not
enforce version wiring, the action-to-job output mappings, or the runtime
comparison. Extend the incumbent checks/tests where these invariants need
enforcement; do not treat a syntactic caller check as end-to-end proof.

A GitHub Release can exist even when GCP publication fails. Define recovery
against the original version and source SHA; rerunning release creation may
produce no new `release_created` output. A dispatch against today's main is not
proof of repairing an older release. Keep failure visible and retain immutable
version/commit bindings. Exercise retry and overlapping automatic/manual
publication: the constant `release-gcp` concurrency group needs evidence that
each release is published, without pending work being superseded or a caller
blocking its own callee. Do not assume `cancel-in-progress: false` proves this.

Extensibility already has the right seams: explicit version/source inputs,
runtime registry coordinates, deployment-owned numeric trust anchors, and the
canonical `RELEASE_IMAGE_NAMES` / manifest contract. A later owner or registry
change belongs at those seams, not in branch-specific publishers or new schema
copies. Any future image-set change must update the existing contract and its
consumers coherently; a transfer does not require one.

Required evidence includes positive automatic and manual runs, denied refs and
callers, empty/mismatched revisions, malformed/multiline versions, wrong
owner/digest attestations, scan failures, retry, and overlapping release behavior.
Reuse `test_release_workflow.py`, `test_repository_identity.py`, installation
manifest/settings tests, and bootstrap release-gate tests. Run the full CI-level
ADR guard and `actionlint` for workflow changes; Terraform changes also need
native tests, the WIF checker, and repository TFLint checks. Local tests cannot
prove GitHub settings, Sonar activation, public package access, or tenant trust
cutover: record authorized readback and hosted evidence for those claims.

Non-goals: implementing the issue during this preflight; performing a transfer,
publication, settings change, cloud apply, or tenant update; redesigning release
versioning, product names, application controllers/DTOs/database persistence,
adapter boundaries, or scan policy. No new service, secret store, trust bypass,
workflow engine, or parallel validator is justified.
