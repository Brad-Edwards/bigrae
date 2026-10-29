---
name: gcp-tenant-operate
description: Install, update, smoke-test, diagnose, and tear down a GitHub-less GCP BigRAE deployment with deploy.py (ADR-070). Use when the user asks to deploy, upgrade, destroy, or debug a GCP tenant.
---

# Operate a GCP tenant

This skill is the agent workflow for one GCP deployment. The procedure and
the facts are in these documents. Read the section that you need; do not work
from memory:

- [GitHub-less GCP deployment guide](../../../docs/technical/handbook/gcp-github-less-deployment.md):
  foundation, release, bundle, install, update, destroy.
- [RB023](../../../docs/runbooks/rb023-gcp-deployment.md): failure symptoms,
  investigation commands, and resolution by stage.
- [GCP tenant standup gotchas](../../../docs/dev/gcp-tenant-standup-gotchas.md):
  operator procedure that is not in code.
- [Design](../../../docs/architecture/github-less-gcp-deployment-design-2571.md).

Use the `gcp-release` skill to publish the release that you deploy.

## Rules

- Get the tenant inputs from the user: project ID, deployment name, hostname,
  operator email, release revision or deployer digest, and range image
  location. Keep them out of commits, issues, PRs, and docs. The repository is
  public.
- For each failure, first check `origin/dev` and the issue tracker for an
  existing fix or issue. Reuse an existing issue. Then fix the cause, add
  tests, and open a PR to `dev`. Close another issue that your fix resolves.
- Never merge into `dev` or `main`, and never close a PR. The user does that.
- Never weaken a gate to pass it: no ignore entries, no skipped scans, no
  bypass flags. Fix the finding.
- Hand-patch a running test cycle only after the fix is in a PR (gotchas:
  "Hand-patch a checkout deploy during a test cycle").
- Do not change the global gcloud account. Pin it per command with
  `CLOUDSDK_CORE_ACCOUNT`. When gcloud reports `Reauthentication failed`, ask
  the user to run `gcloud auth login` and `gcloud auth application-default
  login`.

## Run the commands

1. Use the deployer image of the release when Docker is available.
2. Without Docker, use a clean worktree of the release revision and the tool
   versions that `platform/deployer/gcp/Dockerfile` pins. Run each command as
   `uv run --project scripts/bootstrap scripts/bootstrap/deploy.py <command>`.
   Read the manifest from the deployer image (gotchas: "Read the release
   manifest without Docker") and pass it with `--release-manifest`.
3. Run a deploy or a destroy in the background with its output in a log file.
   Report the stage lines: `Infrastructure:`, `Rollout:`, `✓`, `✗`, and `⚠`.
   A first install takes about 40 minutes; GKE is the slowest part.
4. When a command stops on a lock that you know is stale, run the next command
   with `--break-lock`.

## Lifecycle

| Step | Command | Check |
| --- | --- | --- |
| First install | `gcp-deploy --store gs://<bucket> --first-install` | Report the printed ingress address. The user points the DNS A record at it, unproxied. |
| Certificate and smoke | `gcp-deploy --store gs://<bucket>` | The certificate becomes `ACTIVE` and the post-deploy smoke passes. The smoke creates and destroys a range. |
| Update | `gcp-deploy` with the manifest of the newer release | `Running workload images match the approved release` and the smoke pass. |
| Teardown | `gcp-destroy --store gs://<bucket> --confirm <name>` | Only the foundation, the release registry, the KMS key rings, the operator secret, and range images stay. |
| Reinstall | `gcp-deploy --first-install` | The ingress address is new; the DNS record must change. |

## Diagnose by stage

| Stage | Where to look |
| --- | --- |
| Inputs, lock, identity | The command output. RB023 Investigation steps 1 to 4. |
| Terraform | The apply output, which shows each retried error. RB023 for a state lock (force-unlock takes the lock-object generation) and a node-pool stockout. |
| Scan | `release-scans/<revision>/` in the release-evidence bucket. Fix the vulnerable dependency; then publish a new release. |
| Rollout | After an atomic Helm rollback the pods are gone; read the container logs in Cloud Logging (RB023). For a denied Job create, read the GKE audit log for `jobs.create` in the jobs namespace. Check a policy change with `kubectl apply --dry-run=server`. |
| Smoke | The `worker-provisioner-launcher` logs and the provisioner Job pods in the jobs namespace. |
| Certificate | RB023 and the gotchas: `FailedNotVisible` can last about 45 minutes after the record resolves. |
