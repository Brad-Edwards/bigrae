# RB012: Portal installation or deployment

Applicability: protected GCP tenant environments and AWS dev where configured.
Operator: release/platform owner. Verified: 2026-10-23.

## Symptom

A new installation or upgrade is required, a deployment failed, or the running
portal revision does not match the approved release.

## Investigation

1. Record the intended Git revision, exact image digests, selected protected
   environment, approved configuration revision, Terraform plan/run, and last
   successful deployment. Confirm that the selected environment is one of the
   workflow's closed choices.
2. Validate `shifter.yaml` without exposing referenced secrets:

   Read-only command:

   ```bash
   uv run --project shifter/installation shifter-config validate shifter.yaml
   uv run --project shifter/installation shifter-config doctor shifter.yaml
   ```

3. Inspect the latest `Deploy` workflow gates: quality, provider validation,
   Terraform plan/apply, image build/scan, migration, Helm rendering, rollout,
   certificate/readback, and post-deploy smoke.
4. For a GCP deployment without GitHub, read the `gcp-deploy` output and its
   evidence. The scan evidence is under `release-scans/` and the running-image
   evidence is under `deployments/` in the release-evidence bucket. See
   [Evidence](../technical/handbook/gcp-github-less-deployment.md#evidence) in the GitHub-less
   GCP deployment guide. For a failed `gcp-deploy` run, use
   [RB023](rb023-gcp-deployment.md).
5. The current top-level workflow has no generic `prod` choice. A child
   workflow's support for a profile is not authorization to invent a target or
   run local production deployment commands.

## Resolution

Dispatch `.github/workflows/deploy.yml` from the approved revision and select
the approved protected environment. For first bootstrap, follow the published
GCP installation path and its certificate/DNS gate. For rollback, deploy an
exact previously verified revision and digest through the same workflow; do not
retag images or apply local Helm/Terraform. Verify rollout, running digests,
managed certificate, `/health/`, worker heartbeats, and post-deploy smoke.

For a GCP deployment without GitHub, run `gcp-deploy` from the deployer image
of the approved release
([GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md)). Use
`--first-install` until DNS points at the ingress address. To roll back, run
the deployer image of an earlier verified release.

## Prevention

Retain release evidence, protected-environment approvals, immutable digests,
saved Terraform plans, migration results, and smoke output. Rehearse bootstrap
and rollback against a clean owned environment. See [GCP first
run](../technical/handbook/gcp-first-run.md) and the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md).

Escalate when the target is absent from the closed workflow choices, the exact
artifact identity is unavailable, a gate is bypassed, or rollback would require
a data downgrade.
