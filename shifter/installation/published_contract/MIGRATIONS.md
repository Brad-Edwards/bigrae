# Backend-bundle contract migrations

This directory holds the published, versioned backend-bundle contract (issue #1323).

- `backend-bundle-contract.json`: the current published artifact (the contract version, the
  supported versions, the `BackendBundle` JSON schema, and the registered backends). It is
  **generated** from `installation.contract` and `installation.registry`; never hand-edit
  it. Regenerate with `shifter-config contract export`. CI (`installation` test lane) fails
  if it drifts from the code.
- `backend-bundle-contract.v<N>.json`: the **frozen** snapshot of contract version `N`,
  minted the first time version `N` is published. Snapshots are immutable: `export` never
  overwrites one, so a published version's shape cannot silently change. The breaking-change
  gate compares the current artifact against the frozen snapshot of the current version.

The published version is the backend `contract_version`
(`installation.contract.SUPPORTED_CONTRACT_VERSIONS`), independent of `RootConfig.version`
and of the `installation` package version.

## Contract version 1 additive lifecycle metadata (issue #1324)

`BackendBundle` now publishes optional `deploy` and `teardown` `CommandSpec`
entrypoints. Existing version-1 bundle records remain valid because both fields
default to `null`; bundle authors may add them when their lifecycle is available
through a structured, shell-free argv command whose executable is declared in
`required_tools`.

## Changing the contract

- **Additive change** (new optional field, new enum value, new backend): edit
  `contract.py` / `registry.py`, run `shifter-config contract export`, and commit the
  updated `backend-bundle-contract.json`. The current version's frozen snapshot stays as-is
  (the change remains backward-compatible with it), so no version bump is required.
- **Backward-incompatible change** (remove a field, remove an enum value, make a field
  required): the breaking-change gate fails against the frozen snapshot unless you, in the
  same change:
  1. bump the version by adding the new value to `SUPPORTED_CONTRACT_VERSIONS` in
     `contract.py` and `_CONTRACT_VERSION` in `registry.py`;
  2. add a `## Contract version <N>` migration note below describing the break and how a
     consumer migrates;
  3. run `shifter-config contract export`, which regenerates the artifact and mints the new
     `backend-bundle-contract.v<N>.json` frozen snapshot. The prior version's snapshot is
     left untouched.

Run `shifter-config contract check` to verify drift, compatibility, and registry
conformance locally before pushing.

## Contract version 1

Initial publication of the backend-bundle contract as a committed, versioned artifact.
Covers the `aws` and `gcp` bundles. Completing a provisional backend into its closed
settings model (AWS #728, GCP #729) publishes that backend's `settings_schema` from `null`
to a concrete schema. This is treated as an additive completion of the documented
migration-debt placeholder rather than a version bump: the `null` provisional schema was a
placeholder, never a promise to accept arbitrary settings, and the compatibility gate does
not flag a `null` → concrete `settings_schema` transition. No prior version; nothing to
migrate.

## Contract version 1 external runtime inventory cutover

The current inventory drops retired pack-specific build and credential environment
keys. This changes the generated runtime metadata; the version-1 settings schema
is unchanged, and its frozen snapshot remains untouched. Packs and executable
adapters are installed independently through the tenant administration UI.
Existing AWS ranges that depend on legacy guest model roles must be drained with
the previous release before the new platform IAM is applied. Broker qualification
is required before migrating workloads that need model access.

## Contract version 1 deployment profile retirement

The AWS registry now admits only the maintained `dev` and `prod` profiles.
The retired environment has no deployed resources, as confirmed by its operator.
Existing configurations for that environment must select a maintained profile and supply its own deployment inputs.
The bundle schema and settings schema remain unchanged.
The current artifact is regenerated; the version-1 frozen snapshot remains unchanged.
Any archived cleanup uses the earlier release with its original backend, under operator control.

## Contract version 1 GitHub-less GCP deployment (issue #2571)

The GCP settings schema adds optional fields for the GitHub-less deploy command:
`range_cells`, `participant_openvpn`, `ctf_content_bucket`, `operator_bootstrap`,
`post_deploy_smoke`, and `release_trust`. Existing version-1 records remain valid because each new field is optional.
The shared Helm chart is now the only GCP Kubernetes renderer.
The GCP bundle no longer owns `platform/k8s/gcp` or its README, and the `kustomize-render` validation check is removed.
The `helm-template` check renders the chart with the GCP profile values, and the `kube-linter` check lints the chart.
The required tools replace `docker` with `oras` and `trivy`, because a deploy copies and scans release images and builds none.
The GCP bundle also owns `platform/deployer/gcp`, `.github/workflows/gcp-dev-destroy.yml`, `.github/workflows/release-gcp.yml`, `docs/technical/handbook/gcp-github-less-deployment.md`, and `docs/runbooks/rb023-gcp-deployment.md`.
The current artifact is regenerated; the version-1 frozen snapshot remains unchanged.

## Contract version 1 GCP image workflows (issue #2668)

The GCP bundle no longer owns `.github/workflows/gcp-dev-destroy.yml`, which is removed with the retired GitHub-deployed tenants.
It now owns `.github/workflows/packer-gcp.yml` and `.github/workflows/packer-gcp-validate.yml`, which build, validate, and publish the guest images of a release.
The bundle schema and settings schema remain unchanged.
The current artifact is regenerated; the version-1 frozen snapshot remains unchanged.
