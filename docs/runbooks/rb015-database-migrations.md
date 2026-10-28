# RB015: Database migrations

Applicability: GKE and EKS. Operator: release and data owner. Verified:
2026-10-23.

## Symptom

A deployment is blocked on migrations, a migration Job fails, or application
pods report schema mismatch.

## Investigation

1. Record the release revision, portal image digest, current schema/migration
   state, migration Job name, and target database.
2. Inspect the dedicated migration Job, its events and bounded logs. Long-lived
   web and worker pods set `SKIP_MIGRATIONS=1`; they must not race the migrator.
3. Confirm the Job uses the dedicated migrator service account/database duty,
   the same image digest as the release, and the provider renderer selected by
   the deployment workflow. On GCP, the `gcp-deploy` command renders and runs
   the migration Job (`platform-migrate`) before the Helm upgrade, for the CI
   path and for the path without GitHub.
4. Check database health, locks, available storage, statement runtime, backward
   compatibility, and whether application rollout began.

## Resolution

Repair the migration or dependency in source and rerun the protected deployment
workflow, which renders and executes the single migration Job before rollout. A
GCP deployment without GitHub reruns `gcp-deploy` with the deployer image of the
fixed release ([GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md)).
Do not run `manage.py migrate` from a web/worker pod, use runtime credentials,
or start concurrent migration Jobs. Roll back application code only when the
schema remains compatible; data/schema rollback requires an approved recovery
plan. Verify migration state, rollout, health, workers, and smoke.

## Prevention

Keep migrations forward-compatible across the deployment window, test them on a
representative restore, bound lock-heavy changes, and retain migration Job
evidence. Monitor database headroom before release.

Escalate destructive or long-running migrations, a partially applied migration,
unknown schema ownership, or rollback requiring data restoration.
