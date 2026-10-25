# RB005: Database connection errors

Applicability: Cloud SQL primary; RDS optional. Operator: platform and data
owner. Verified: 2026-10-23.

## Symptom

Portal or worker logs report database connection failures, migrations cannot
connect, or Cloud SQL/RDS reaches connection or resource limits.

## Investigation

1. Determine the failing duty: portal runtime, worker, migrator, Guacamole,
   OpenFGA, or provisioner. Their identities and database privileges are not
   interchangeable.
2. Check database instance state, private-network reachability, proxy/sidecar
   health, certificate/IAM errors, active connections, storage, CPU, memory, and
   recent maintenance. Inspect Kubernetes rollout and replica changes in the
   same window.
3. Confirm the runtime uses the deployed Cloud SQL Auth Proxy/native IAM or RDS
   IAM/password boundary. Inspect secret references and version metadata only;
   never print the credential or DSN.
4. Check whether a migration Job, restore, failover, or provider quota event is
   in progress before restarting clients.

## Resolution

Repair the owning dependency or identity through Terraform and the protected
deployment workflow. If clients hold stale connections after the database is
healthy, redeploy the affected workload through the same workflow, one duty at
a time, and watch connection counts. Do not share a more privileged login,
disable TLS, expose the database publicly, or raise `max_connections` as the
first response. Validate `/health/`, worker heartbeats, a read-only application
path, and pending operation processing.

## Prevention

Capacity-plan each database duty and workload replica count, alert on connection
headroom and proxy failures, and exercise backup restoration. The application
uses short-lived Django connections; investigate leaks or unexpected replica
growth before changing server limits.

Escalate before failover, restore, parameter changes, connection-limit changes,
or any access-boundary change.
