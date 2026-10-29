# Build and Deployment Provenance

Status: active

Issue: [#1519](https://github.com/Brad-Edwards/shifter/issues/1519)

ADR: [ADR-037](../adr/index.yaml)

This is the operator reference for the supply chain provenance controls Shifter
applies to its build and deployment pipelines. The trust-boundary rationale is in
[`rev1-build-deployment-provenance-preflight-1519.md`](rev1-build-deployment-provenance-preflight-1519.md);
the enforced rules are `ADR-037-R1..R6`.

## Controls at a glance

| Control | Where | Enforcement |
| --- | --- | --- |
| Third-party actions pinned to a full commit SHA | credentialed `.github/workflows/*` | `workflow-action-sha-pinning` adr_guard check (`ADR-037-R1`), fail-closed |
| Container bases pinned to `@sha256` digests | every committed `Dockerfile` `FROM` | digest-only `FROM` + Dependabot `docker` (`ADR-037-R2`) |
| Downloaded CLIs integrity-verified | provisioner `terraform` (and `kubectl`/`virtctl` in the GDC build), `kubeconform`, `kube-linter` | `sha256sum -c` before use (`ADR-037-R3`) |
| Python runtime deps hash-pinned | provisioner image | `pip install --require-hashes` from reviewed locks (`ADR-037-R4`) |
| SBOM + signed provenance for release images | portal, provisioner, guacd, guacamole-client builds | `docker/build-push-action` `provenance: mode=max` + `sbom: true`, then `actions/attest-build-provenance` (`ADR-037-R5`) |
| Deploy verifies attestation before rollout | AWS ECS (portal, provisioner, guacd, guacamole-client) | `gh attestation verify oci://<image@digest> --repo Brad-Edwards/shifter` (`ADR-037-R6`) |
| Deploy verifies attestation before rollout | GKE (portal, provisioner, guacd, guacamole-client, openvpn) | `gcp-deploy` verifies the registry-stored Sigstore bundle of each digest with the Sigstore trust root, with no GitHub API (`ADR-037-R6`, ADR-070) |

## Refresh procedures

### GitHub Actions SHA pins

Dependabot's `github-actions` ecosystem opens PRs bumping the pinned SHA and its
`# <version>` comment together. The `workflow-action-sha-pinning` check keeps
every credentialed workflow SHA-pinned; a Dependabot PR that only moves a tag
would fail the check, so pins stay full SHAs.

### Container base-image digests

Every `FROM` is `image@sha256:<digest>` with the human-readable tag on the
preceding `# base image:` comment line. Digest-only (no tag on the `FROM`)
satisfies SonarCloud `docker:S8431` ("specify either version tag or digest").

To refresh a base image digest, resolve the current digest for the recorded tag
and update both the `FROM` and its comment:

```sh
docker buildx imagetools inspect <image>:<tag> --format '{{.Manifest.Digest}}'
```

The `.github/dependabot.yml` `docker` ecosystem covers every image directory.
Note that Dependabot's digest-only refresh is best-effort: it opens an initial
PR but does not reliably supersede it as newer digests ship
([dependabot-core#7387](https://github.com/dependabot/dependabot-core/issues/7387)),
and it cannot add a digest to a tag-only pin. Treat the command above (run at a
reviewed cadence, or when a CVE prompts a rebuild) as the authoritative refresh;
Dependabot is the reminder, not the guarantee.

### Downloaded CLIs

`shifter/engine/provisioner/Dockerfile` verifies `terraform` against
HashiCorp's `SHA256SUMS`. Only the GDC build (`INSTALL_KUBEVIRT=true`)
installs `kubectl`, verified against the published `kubectl.sha256`, and
`virtctl`, verified against a pinned SHA-256 value. The GCP deployer
image (`platform/deployer/gcp/Dockerfile`) verifies `terraform`, `helm`,
`kubectl`, `trivy`, and `oras` against SHA-256 values pinned in that
Dockerfile. Change each version and its SHA-256 value together. It builds
`gke-gcloud-auth-plugin` from a pinned `kubernetes/cloud-provider-gcp` commit,
checks the fetched commit, and relies on `go.sum` for each module.
`kubeconform` is verified against its release `CHECKSUMS` manifest. `kube-linter` publishes a
cosign `.sig` but no SHA manifest, so its tarball sha256 is pinned in
`_quality.yml` (`KUBE_LINTER_SHA256`) and verified before extraction; bump it
with the version.

### Python locks (provisioner)

`requirements.lock` and `requirements-gcp.lock` are hash-pinned. Regenerate from
`shifter/engine/provisioner/`:

```sh
uv export --no-emit-project --frozen --no-dev -o requirements.lock
uv export --no-emit-project --frozen --no-dev --no-hashes > /tmp/constraint.txt
uv pip compile <gcp-extras> --constraint /tmp/constraint.txt \
  --generate-hashes --python-version 3.12 --no-header -o requirements-gcp.lock
```

The GCP extras (`google-*`, `pycdlib`) are compiled against the frozen main lock
so shared transitive dependencies stay at the locked versions. `kubernetes` is a
main dependency and is intentionally not duplicated in the GCP lock.

## Attestation and verification

Release image builds embed an SBOM and maximum BuildKit provenance and create a
GitHub OIDC-signed attestation (`actions/attest-build-provenance`) bound to the
fully qualified image name and the exact `@sha256` digest. The AWS deployment verifies
that attestation with `gh attestation verify` against the fixed
`Brad-Edwards/shifter` repository identity and the exact digest before it mutates
any runtime. Any verifier failure, missing attestation, repository mismatch, or
digest mismatch is a hard `::error::` and non-zero exit; there is no bypass.

The image-building reusable workflows request the `attestations` token scope, so
`deploy.yml` grants `attestations: write` to the jobs that call
`_shifter-engine.yml`, `_shifter-platform.yml`, and `_gcp-dev.yml`. A reusable
workflow cannot request a broader `GITHUB_TOKEN` scope than its caller, so
omitting this grant fails the run at startup.

The GCP path stores each attestation with its image (#2571, ADR-070). The
release workflow (`release-gcp.yml`) and the CI build in `_gcp-dev.yml` attest
with `push-to-registry: true`, so the Sigstore bundle is an OCI referrer of the
image digest. `gcp-deploy` reads the referrers from the registry and verifies
each bundle with the Sigstore public-good trust root. The policy requires:

- the GitHub Actions OIDC issuer;
- the immutable source repository ID and owner ID (`settings.release_trust`, or
  the GitHub context in CI);
- a source revision equal to the release manifest revision.

The in-toto statement must name the exact image digest and use the SLSA
provenance v1 predicate. Any failure stops
the deploy before the rollout; there is no bypass.

## Scope: OCI images vs. Packer VM images

The attestation and verification controls apply to the **OCI** release images
(portal, provisioner, guacd, guacamole-client; on GCP also openvpn and the
deployer image). Packer AMI/GCE artifacts are
**not** OCI images and are **not** represented as OCI provenance. Their
credentialed workflows are covered by the action SHA-pin control (`ADR-037-R1`);
attesting the immutable Packer manifest keyed by the AMI/GCE image identifier is
a tracked follow-up (see below), not a claim made by the OCI attestation flow.

### Guacamole image identity

guacd / guacamole-client are version-pinned upstream bases on immutable ECR
repositories. To attest honestly on immutable ECR, each is tagged
`<version>-<git-tree-hash>` of its build context, so the tag changes only when
the image content changes: content changes yield a freshly built, attested image;
unchanged content reuses the already-attested one. The guac module's
`data.aws_ecr_image` resolves that tag to its immutable `@sha256` digest and pins
it in the task definition; the deploy resolves the same deterministic tag, so the
attested digest is the deployed digest.

## Tracked follow-ups

These are recorded here so the boundaries are explicit, not silent:

- **OS bootstrap scripts and Packer installers.** `curl | sh` bootstraps
  (`get.docker.com`, NodeSource) in the scenario-bake workflows and the Packer
  SSM-agent / Windows installer downloads are OS-package-manager territory; the
  correct control is a signed package repository rather than a checksum, tracked
  separately from the CLI-binary verification landed here.
