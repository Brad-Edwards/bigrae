# ADR-070: GCP deployments run from published releases without GitHub

## Status

Accepted 2026-10-28. Amends ADR-003-R6, ADR-004-R5, ADR-004-R23, ADR-006,
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
   through `workflow_dispatch` from protected `refs/heads/main`, `refs/heads/dev`,
   or `refs/heads/deploy-test`. It runs on
   GitHub-hosted runners in the `gcp-release` Environment. It builds the five
   control-plane images (portal, provisioner, guacd, guacamole-client, and
   openvpn) and the deployer image. It pushes each image by digest to the
   release registry. For each digest it signs build provenance with an SBOM and
   stores the attestation with the image (`push-to-registry: true`). The release
   manifest (`bigrae.gcp-release.v1`) inside the deployer image names the source
   revision, the chart version, and the exact image identities. Before it attests
   the deployer image, the workflow scans that exact digest with the blocking
   Trivy gate. The only accepted exposures are time-boxed vendor-binary entries
   in `platform/deployer/gcp/trivyignore.yaml`. Each entry stops applying on its
   expiry date.
   The exact `gke-gcloud-auth-plugin` Linux binary has a reviewed false positive
   for [CVE-2026-56857](https://pkg.go.dev/vuln/GO-2026-6604): the official Go
   advisory limits both affected imports to Windows, while the verified Google
   archive reports `GOOS=linux`. The entry names only that CVE and binary path,
   is owned by `@Brad-Edwards`, and expires on 2026-11-30. The vendor manifest and
   archive checksum remain pinned; other findings still block the image gate.
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
   exact digest subject, and the SLSA provenance v1 predicate. After rollout, the command
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
   the destroy workflow. The CI destroy workflow keeps its protected-ref check.
8. **Base guest images come from an explicit source.** `deploy.py gcp-images`
   imports from an explicit `--source` registry prefix. It keeps the digest pins
   and the OCI contract checks, and it writes the exact image references to
   `range-images.json`. When `GCP_RELEASE_REGISTRY` is set, `packer-gcp.yml`
   copies each published base-image artifact by digest into the release registry.

Catalog initialization is an application operation after verified native
activation and readiness. It uses the existing content services with an actual
native bearer proof supplied through `--catalog-credential-file`; cloud IAM and
deployment control do not grant application permission. The schema Job must not
create a legacy catalog actor or unscoped content. Missing proof leaves catalog
setup explicitly incomplete while the active application remains available for
administrator login. Private credential files and transient Secret mounts stay
outside the deployment input bundle and logs.

## Consequences

- An operator can deploy, update, and tear down a GCP deployment from a release
  in the release registry, with no GitHub, GHCR, or github.com access.
- A deployment trusts releases through `settings.release_trust`, not through a
  GitHub token or the GitHub API.
- CI deploys and operator deploys run the same code, the same gates, and the same
  renderer.
- Existing CI deployments keep their workflows, Environments, and committed
  Terraform values. Their first deploy after this change adopts the objects that
  Kustomize applied into the Helm release.
- The `release-workflow-provenance` and `workflow-action-sha-pinning` ADR guard
  checks and `scripts/check_tf_gcp_wif_trust` enforce the decision. The deploy
  workflow guard tests, the bootstrap unit tests, and the native Terraform tests
  of the identity and release-registry roots also enforce it.
