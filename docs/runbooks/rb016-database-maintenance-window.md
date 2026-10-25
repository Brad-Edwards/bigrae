# RB016: Database maintenance window

Applicability: Cloud SQL primary; RDS optional. Operator: data and platform
owner. Verified: 2026-10-23.

## Symptom

A provider maintenance event is scheduled or active, pending modifications
exist, or database availability changes during maintenance.

## Investigation

1. Record provider event ID, database identity, engine/version, scheduled window,
   affected changes, expected interruption, backup status, and application load.
2. Check connection headroom, active migrations/restores, range operations,
   worker backlog, and deployment activity. Avoid overlapping mutations.
3. For AWS, inspect the configured RDS maintenance/backup windows and pending
   modifications. The deployment checks pending changes with environment-specific
   policy.
4. The GCP Terraform module does not declare an equivalent maintenance-window
   contract. Report the provider event and this gap; do not claim Terraform
   controls the window.

## Resolution

Pause conflicting release and administrative work. Use provider-supported
rescheduling only with data-owner approval. During impact, keep new range
admission bounded and watch database, proxy, portal, and workers. Afterward,
verify instance state/version, connections, schema, `/health/`, worker backlog,
and a read-only application path. Reconcile pending modifications through
Terraform and the protected workflow.

## Prevention

Track provider events, define service maintenance communications and change
freeze windows, test client reconnection, and keep backup/restore evidence
current. Add a Terraform-managed GCP window only through separate reviewed
architecture work.

Escalate unexpected version changes, maintenance outside the agreed window,
failed reconnection, pending destructive modifications, or missing backup
evidence.
