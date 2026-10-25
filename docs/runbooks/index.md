# Operations runbooks

These runbooks are the operational handoff for the current BigRAE control
plane. GCP with GCE range cells is the primary supported release path. AWS
procedures are called out only where the repository and a deployed AWS tenant
provide an equivalent surface. GDC VM Runtime remains an operator-validation
mode and is not a live-fire containment boundary.

The procedures were checked against the repository and deployed service types
on 2026-10-23. Resolve names from the selected deployment's Terraform outputs
and protected environment; never copy names from an example or another tenant.

## Shared operating contract

Set non-secret identifiers before using command examples:

```bash
export PROJECT_ID="<gcp-project-id>"
export REGION="<gcp-region>"
export CLUSTER="<gke-cluster>"
export NAMESPACE="<platform-namespace>"
export JOBS_NAMESPACE="<jobs-namespace>"
export REQUEST_ID="<request-uuid>"
export OPERATION_ID="<operation-uuid>"
export SINCE="2h"
```

For AWS-only branches, use the approved profile and set `AWS_REGION` and
`AWS_CLUSTER`. Confirm the active cloud identity, selected project/account,
cluster context, namespace, incident window, and operator authorization before
investigation. Production is never an implicit default.

Investigation is read-only. Keep request and operation IDs in evidence, but
redact user data, tenant identifiers, provider payloads, signed URLs, database
connection strings, and credential material. Never print Kubernetes Secrets,
Secret Manager values, process environments, or complete Terraform outputs.

Use the application service boundary for range, account, upload, and credential
changes. Use the protected deployment workflow for infrastructure, image,
migration, or workload changes. Direct SQL, hand-built events, local production
Terraform/Helm, arbitrary `kubectl exec`, and deleting cloud resources by name
are not recovery paths.

Escalate when the safe mutation surface is absent, ownership is ambiguous,
cleanup cannot be proven, a security boundary may have failed, or recovery
would change infrastructure outside the selected deployment. The incident
commander owns service restoration; the security lead owns containment and
credential revocation; the platform owner owns deployment and Terraform; the
data owner approves restore/cutover; the range owner confirms lifecycle and
residual inventory.

Treat application quotas, cloud service quotas, event-capacity profiles,
model-access budgets, and billing budgets as separate limits. A limit change is
a reviewed configuration or infrastructure change with an owner, expiry or
review date, cost approval, and post-change readback. Do not solve capacity by
silently weakening containment, retention, admission, or cleanup checks.

## Quick paths

| Need | Start here |
| --- | --- |
| Install, upgrade, or roll back | [RB012](rb012-portal-deployment.md), [RB013](rb013-infrastructure-deployment.md), [RB015](rb015-database-migrations.md) |
| Back up, restore, and reconcile | [RB014](rb014-database-backup-restore.md), [RB003](rb003-orphaned-range-resources.md) |
| Queue or provider dependency outage | [RB008](rb008-dead-letter-messages.md), [RB020](rb020-provisioner-timeout.md) |
| Sensor or collector loss | [RB007](rb007-logs-not-flowing.md) |
| Quarantine or revoke credentials | [RB009](rb009-user-locked-out.md), [RB002](rb002-range-teardown-stuck.md) |
| Stuck, cancelled, or expired range | [RB001](rb001-range-provisioning-failed.md), [RB002](rb002-range-teardown-stuck.md), [RB017](rb017-stale-range-cleanup.md) |
| Residual inventory | [RB003](rb003-orphaned-range-resources.md) |
| Capacity or cost limit | [RB011](rb011-storage-quota-exceeded.md), [RB018](rb018-container-registry-cleanup.md), [RB019](rb019-check-system-health.md) |
| Escalation ownership | [Shared operating contract](#shared-operating-contract) and the Escalate condition in each runbook |

## Catalog

| ID | Incident |
| --- | --- |
| [RB001](rb001-range-provisioning-failed.md) | Range provisioning failed |
| [RB002](rb002-range-teardown-stuck.md) | Range teardown stuck |
| [RB003](rb003-orphaned-range-resources.md) | Orphaned range resources |
| [RB004](rb004-portal-down.md) | Portal unavailable |
| [RB005](rb005-database-connection-errors.md) | Database connection errors |
| [RB006](rb006-worker-errors.md) | Worker or provider-function errors |
| [RB007](rb007-logs-not-flowing.md) | Logs or containment signals not flowing |
| [RB008](rb008-dead-letter-messages.md) | Dead-letter messages |
| [RB009](rb009-user-locked-out.md) | User locked out or credential revoked |
| [RB010](rb010-agent-upload-failed.md) | Agent upload failed |
| [RB011](rb011-storage-quota-exceeded.md) | Upload storage quota exceeded |
| [RB012](rb012-portal-deployment.md) | Portal installation or deployment |
| [RB013](rb013-infrastructure-deployment.md) | Infrastructure deployment |
| [RB014](rb014-database-backup-restore.md) | Database backup, restore, and reconciliation |
| [RB015](rb015-database-migrations.md) | Database migrations |
| [RB016](rb016-database-maintenance-window.md) | Database maintenance window |
| [RB017](rb017-stale-range-cleanup.md) | Stale range cleanup |
| [RB018](rb018-container-registry-cleanup.md) | Container registry cleanup |
| [RB019](rb019-check-system-health.md) | System health check |
| [RB020](rb020-provisioner-timeout.md) | Provisioner timeout |
| [RB021](rb021-high-database-connections.md) | High database connections |
| [RB022](rb022-firehose-delivery-lag.md) | AWS Firehose delivery lag |

Detailed current-state ownership is in the [architecture
handbook](../technical/architecture.md). Existing provider procedures under
`docs/ops/` and `docs/dev/` remain authoritative for their specific surfaces.
