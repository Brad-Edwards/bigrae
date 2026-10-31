# ADR-070: GCP deployments run from published releases without GitHub

## Status

Accepted 2026-10-28. Amended 2026-10-29 by
[#2659](https://github.com/Brad-Edwards/bigrae/issues/2659): the release
registry is public. Amended 2026-10-30 by
[#2679](https://github.com/Brad-Edwards/bigrae/issues/2679): one command
installs a tenant from a release and one bundle. Amends ADR-003-R6, ADR-004-R5, ADR-004-R23, ADR-006,
ADR-007, ADR-033-R2, ADR-037, ADR-053-R3, and ADR-069-R3. Issue
[#2571](https://github.com/Brad-Edwards/bigrae/issues/2571) records the work.
The [design](https://github.com/Brad-Edwards/bigrae/blob/dev/docs/architecture/github-less-gcp-deployment-design-2571.md)
records the architecture, the data flows, and the security controls.

## Context

Before #2571, each GCP deployment update depended on GitHub:

- `.github/workflows/_gcp-dev.yml` held most of the deploy logic as inline
  shell and Python steps.
- GitHub Environments held the root config, the project, the hostname, the
  operator password, the image references, and the range settings.
- GitHub OIDC tokens were the only path to the purpose identities, and a
  self-hosted runner ran each deploy.
- The deploy verified image provenance through the GitHub attestation API.
- Each deploy built the control-plane images from a repository checkout.
- A new deployment needed committed Terraform values, a Kustomize overlay, and
  workflow entries in this repository.

The local `gdc-bootstrap` path installed without Actions, but it did not run the
ADR-037 release gates. It rendered with the Helm chart, while CI rendered with
Kustomize.

An operator must be able to stand up, update, and tear down a GCP deployment
without GitHub. Issue #2572 tracks the live test of this path. Issue #2573
tracks the AWS path.

## Decision

1. **A release is built once.** `.github/workflows/release-gcp.yml` runs only
   from protected `refs/heads/main`, `refs/heads/dev`, or
   `refs/heads/deploy-test`. Two events start it. The `release-gcp` job of
   `release-please.yml` calls it (`workflow_call`) when a release tag points at
   the pushed `main` commit (#2660). The maintainer dispatches it
   (`workflow_dispatch`) for test builds or to publish a release again. It runs on
   GitHub-hosted runners in the `gcp-release` Environment. It builds the five
   control-plane images (portal, provisioner, guacd, guacamole-client, and
   openvpn) and the deployer image. It pushes each image by digest to the
   public release registry `ghcr.io/<owner>/<repository>` with the workflow
   `GITHUB_TOKEN`. It uses no cloud identity. For each digest it signs build provenance with an SBOM and
   stores the attestation with the image (`push-to-registry: true`). The release
   manifest (`bigrae.gcp-release.v1`) inside the deployer image names the source
   revision, the chart version, and the exact image identities. It also names
   the guest images of decision 8. Before it builds anything, the workflow
   verifies each guest that it names. It never builds, exports, or attests a
   guest image (#2668). Before it attests
   the deployer image, the workflow scans that exact digest with the blocking
   Trivy gate. The only accepted exposures are time-boxed vendor-binary entries
   in `platform/deployer/gcp/trivyignore.yaml`. Each entry stops applying on its
   expiry date.
   The deployer builds `gke-gcloud-auth-plugin` from a pinned upstream commit
   with a digest-pinned Go toolchain. Go verifies the module checksums. The
   plugin has no scan exceptions. Return to the Google binary when it passes
   the blocking deployer scan without plugin-specific exceptions.
2. **One deploy implementation serves every deploy.** `deploy.py gcp-deploy` is
   the deploy for the first install and for each update. An operator runs it from
   the deployer image or from a clean checkout of the release revision. The GCP
   CI workflows call the same commands: `gcp-deploy`, `gcp-release-scan`,
   `gcp-smoke`, and `gcp-destroy`. They hold no inline deploy logic. The Helm
   chart is the only GCP Kubernetes renderer.
   The S8 identity cutover (#2321) uses this same command. A conclusive identity
   probe precedes workload changes. The command drains old application
   processes before migration, runs policy schema/publication and immutable
   administrator bootstrap before activation, and restores application
   workloads only after verification. Reviewed mapping and bootstrap facts
   enter through paired owner-only file paths, never through bundle content.
3. **Deployment inputs stay outside this repository and outside GitHub.** A
   deployment keeps its deployment input bundle in its deployment config bucket
   or in a local directory. The bundle holds `bigrae.yaml`, `terraform.tfvars`
   and `terraform.tfvars.json`, `range-images.json`, and optional inputs. Secret
   values stay in Secret Manager. `bigrae.yaml` refers to them by reference. The
   commands read no per-deployment file from this repository, so a new deployment
   needs no repository change. In CI, `scripts/gcp/render_ci_deployment_inputs.py`
   renders the same bundle from the GitHub secrets and variables and the
   committed per-deployment values. It fails when a GitHub value conflicts with
   the root config.
4. **Operators use the purpose identities through impersonation.** Each command
   stage runs as the deploy, release-scan, or destroy identity through
   service-account impersonation. No service-account key exists. Only the
   principals in `impersonation_principals` receive the token-creator role, each
   only on the identity of its own purpose. GitHub federation is optional: an
   empty `purpose_contexts` creates no Workload Identity pool or provider. CI uses
   `--identity ambient`, because federation already gives each job its purpose
   identity.
5. **The command runs the ADR-037 gates and fails closed.** Before rollout, the
   release-scan identity scans each exact digest for HIGH and CRITICAL findings.
   Every operator rollout scans, also with `--stage rollout`. CI scans in its
   release-scan job, which the rollout job needs.
   The deploy then verifies the Sigstore bundle that is stored with each image.
   The policy binds the GitHub Actions issuer and the immutable repository and
   owner IDs in `settings.release_trust`. It also binds the release revision, the
   exact digest subject, and the SLSA provenance v1 predicate. `gcp-images`
   applies the same policy to each guest image at its recorded build revision
   before it imports anything (#2668). After rollout, the command
   compares each running workload and container with the approved digests. No
   gate has a bypass. The raw evidence and the redacted verdicts go to the private
   release-evidence bucket, not to Actions artifacts.
6. **Release tooling runs one deploy at a time.** In operator mode, `gcp-deploy`
   and `gcp-destroy` run only from tooling whose source revision equals the
   release revision. Every operator stage needs the release manifest. Before any
   change, it checks the tooling revision and verifies the release images. A lock object in the deployment config bucket allows one
   deploy or destroy at a time. Generation preconditions create and remove the
   lock. CI keeps its workflow concurrency group and uses `--lock external`.
7. **Teardown uses the destroy identity.** `gcp-destroy` requires `--confirm`
   with the deployment name. It removes the platform resources and keeps the
   foundation identities and the state, evidence, and config buckets. In
   operator mode, the release tooling check replaces the protected-branch rule of
   the destroy workflow.
8. **Guest images ship as releases (#2668).** A developer builds and validates
   the guest images with their own GitHub repository and a build project that
   they supply. `deploy.py gcp-foundation` creates the build project with only
   the build and validate purposes and writes the `gcp-image-build` and
   `gcp-image-validate` Environments of that repository. `packer-gcp.yml`
   publishes a guest image only after `packer-gcp-validate.yml` passes for the
   exact numeric image ID in the same run. The image goes to
   `ghcr.io/<owner>/<repository>/gce-<role>` with the tag
   `run-<run number>.<attempt>` and a Sigstore attestation of its exact digest.
   Each release names the newest publication of each role in `base_images`
   (decision 10), so a guest changes only when a developer builds it again.
   The release workflow, `gcp-deploy`, and `deploy.py gcp-images` verify every
   role before any change: the artifact contract and a Sigstore bundle that
   satisfies the trust anchor at the build revision of the artifact, from
   `refs/heads/dev` or `refs/heads/main`. The import holds the deployment lock,
   reuses an unchanged image, and writes `range-images.json`. A mirror changes
   only the transport. No BigRAE deployment creates image-build resources. The
   [guest image design](../architecture/gcp-guest-images.md) records the
   components.
9. **The release registry is public.** One release serves every deployment.
   A deployment trusts a release only through the provenance policy of
   decision 5, not through the owner of the registry. The commands send the
   Google access token only to Artifact Registry hosts. Any other registry
   gets anonymous pulls. An operator who cannot reach the public registry
   copies the release images by digest, with their referrers, to a mirror. The
   operator then passes `--release-source`, and the commands verify, scan, and
   import the mirror copy of each digest.
10. **A release installs a tenant with one command.** The release manifest
    pins the base range images by digest and carries the release's OpenFGA
    authorization model. `gcp-install` applies the foundation from the bundle
    and the operator's identity, creates the initial operator password, publishes
    the bundle, and runs the first install. That deploy imports the pinned base
    images as the operator, generates the OpenFGA credentials, and publishes the
    authorization model; each later deploy publishes the release model again
    and reuses an unchanged one. On an empty database, the deploy derives the
    identity facts from the configured operator account. A database with users
    still needs the reviewed legacy conversion facts. A tenant platform creates
    no image-build infrastructure (decision 8). The current release requires native SQL and OpenFGA, so a setting that
    disables either fails before any change.

Catalog initialization is an application operation after verified native
activation and readiness. It uses the existing content services with an actual
native bearer proof; cloud IAM and deployment control do not grant application
permission. The proof is a credential supplied through
`--catalog-credential-file`, or, when the configured operator is the
bootstrap administrator that the cutover recorded, a credential that the
catalog Job issues for that administrator (#2679). That credential holds only
the installation content actions, lasts 15 minutes, never leaves the Job, and
is revoked after the registration. The content services check the
administrator's own policy for each step. The schema Job must not create a
legacy catalog actor or unscoped content. A failed or skipped registration
leaves catalog work pending with a warning while the active application remains
available for administrator login. Private credential files and transient
Secret mounts stay outside the deployment input bundle and logs.

## Environment retirement (#2668)

The GitHub-deployed tenants `balrog`, `gcp-dev`, `orthanc`, and `sauron` do
not exist. Issue #2668 removes their committed values, workflow choices,
GitHub Environments, and the `orthanc` and `sauron` branches. It also removes
`gcp-dev-destroy.yml` and `packer-gcp-promote.yml`. The validated release of
decision 8 replaces promotion. The reusable deploy roots, the generic Helm
profiles, and the deploy commands stay. Issue #2672 decides whether the GitHub
deploy-to-tenant path stays.

## Consequences

- An operator can deploy, update, and tear down a GCP deployment from a public
  release. A deployment that cannot reach GHCR deploys from a mirror, with no
  GitHub, GHCR, or github.com access.
- A new deployment needs no change to the release workflow, no GitHub
  Environment, and no release identity.
- A deployment trusts releases through `settings.release_trust`, not through a
  GitHub token or the GitHub API.
- CI deploys and operator deploys run the same code, the same gates, and the same
  renderer.
- No CI deployment exists after #2668. Every deployment runs the deploy
  commands from a release.
- A release with unchanged guest images carries the same guest digests, and an
  import reuses the images that it created before.
- The `release-workflow-provenance` and `workflow-action-sha-pinning` ADR guard
  checks and `scripts/check_tf_gcp_wif_trust` enforce the decision. The deploy
  workflow guard tests, the bootstrap unit tests, and the native Terraform tests
  of the identity root also enforce it.
