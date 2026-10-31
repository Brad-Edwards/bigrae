---
name: gcp-release
description: Publish and verify a GitHub-less GCP release with release-gcp.yml (ADR-070) and read its deployer digest and manifest. Use when a GCP deploy needs a new release or a release build fails.
---

# Publish a GCP release

A GCP deployment runs only a release that `.github/workflows/release-gcp.yml`
builds and signs. The setup is in the "Publish a release" section of the
[GitHub-less GCP deployment guide](../../../docs/technical/handbook/gcp-github-less-deployment.md).
The release refs and the test-cycle branch are in the "Release from dev or a
deploy-test branch" section of the
[gotchas](../../../docs/dev/gcp-tenant-standup-gotchas.md).

## Rules

- One release serves every deployment. The workflow publishes it to the
  public registry `ghcr.io/<owner>/<repository>`. A new deployment needs no
  workflow or Environment change.
- A deploy-code fix needs a new release, because `gcp-deploy` requires that
  its own revision equals the release revision.
- Release from a ref that the workflow admits: `main`, `dev`, or
  `deploy-test`. Keep `deploy-test` equal to `origin/dev` plus the open fix
  PRs; merge `origin/dev` into it and reapply the open PRs when they drift.
- A failed release scan is a finding to fix, not a gate to relax. Fix the
  image or the tool, open a PR to `dev`, and release again.
- The release does not build guest images. It names the newest verified
  publication of each role (#2668). To change a guest, run `packer-gcp.yml` in
  the developer build project, then release again. See
  [GCP Guest Images](../../../docs/architecture/gcp-guest-images.md).
- The GitHub API rate limit is shared by every session of the account. Poll a
  run every few minutes, not every few seconds.

## Steps

1. Check that the release ref holds every fix that the cycle needs.
2. Start the release. A Release Please release on `main` starts it
   automatically for the tagged commit. When that call fails, or a
   `release-superseded` job names a commit, re-run the Release Please run of
   that commit. For `dev` or `deploy-test`, dispatch it:

   ```bash
   gh workflow run release-gcp.yml --ref <release ref>
   ```

3. Wait for the run. The deployer image build is the longest step, about 35
   minutes.
4. Read the deployer digest from the run log line
   `Attestation created for <registry>/deployer-gcp@sha256:<digest>`.
5. Read the release manifest from the deployer image (gotchas: "Read the
   release manifest without Docker"). Check that `source_revision` equals the
   commit that you released, and that `base_images` and
   `authorization_model` are present.
6. After the first release of a repository, ask the user to make the six
   packages public (gotchas: "Make new release packages public"). Do the same
   for each `gce-<role>` guest package after its first publication.

## When the build fails

| Step | Cause and fix |
| --- | --- |
| Select the guest images | A `gce-<role>` package is missing under `ghcr.io/<owner>/<repository>`, or its newest publication does not verify (gotchas: "Guest images must exist in the release registry"). Build and publish the guest again with `packer-gcp.yml`. |
| Deployer provider mirror | A Terraform root or module lock file does not record a provider. Run `terraform init -backend=false` for that directory and commit the lock change. |
| Scan the deployer image | A tool binary has a fixable HIGH or CRITICAL finding. Bump the tool, or build it from a pinned source commit when no fixed vendor build exists. |
| A build stage that fetches source | Check the pinned commit. An annotated tag object SHA is not the commit SHA. |
| Render the authorization model | The platform environment did not sync, or the OpenFGA CLI image did not run. |
| Control-plane image scan at deploy time | A bundled dependency is vulnerable. Fix it in the image build; see the Guacamole bundled-jar patch for the pattern. |
