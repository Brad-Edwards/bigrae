# RB021: High database connections

Applicability: Cloud SQL primary; RDS optional. Operator: data and platform
owner. Verified: 2026-10-23.

## Symptom

Database connection utilization is high, clients are refused, or connection
pool exhaustion degrades portal and worker operations.

## Investigation

1. Record current/max connections, trend, database CPU/memory, connection
   errors, and recent rollout/autoscaling/maintenance changes.
2. Attribute connections by duty and identity: web, each worker family,
   migrator, Guacamole, OpenFGA, provisioner, and proxy. Do not pool unrelated
   duties under one shared credential.
3. Compare Kubernetes replica counts and restart loops with database sessions.
   Django uses `CONN_MAX_AGE=0`; unexpected persistence or growth needs root
   cause analysis.
4. Check blocked/long transactions, migration activity, leaked clients, proxy
   reconnect storms, and provider limit changes without exposing query text or
   user data in incident records.

## Resolution

Stop the source of runaway connections through its owning deployment or job,
then restore affected workloads gradually through the protected workflow. Fix
leaks, replica configuration, or proxy failure before considering a reviewed
database limit change. Do not restart the database first, terminate sessions
indiscriminately, share credentials, or disable TLS. Verify headroom, normal
request/worker paths, and absence of reconnect growth.

## Prevention

Budget connections per duty and replica, alert on utilization and rate of
change, load-test autoscaling bounds, and monitor long transactions and proxy
health. Include migration and recovery headroom.

Escalate imminent exhaustion, unidentified connection ownership, data-locking
transactions, or any parameter/failover change.
