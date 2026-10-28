# RB019: Check system health

Applicability: GCP primary; AWS optional. Operator: platform on-call. Verified:
2026-10-23.

## Symptom

An operator needs a rapid health assessment before or during an incident,
deployment, event, or capacity change.

## Investigation

Collect each independent signal; do not collapse them into one green status.

1. Public: DNS, certificate, ingress/backend health, and `/health/`.
2. Cluster: node/control-plane status through the approved private access path;
   rollout, pod readiness, restarts, events, and resource pressure.
3. Workloads: `portal-web`, launcher, result applier, outbox drainer, reconciler,
   CMS/Engine/Mission Control workers, Guacamole, OpenFGA, Redis, and database.
4. Queues: Pub/Sub or SNS/SQS backlog, oldest age, dead letters, and worker
   heartbeats.
5. Release: exact running image digests, runtime-config checksum, latest
   deployment readback, migration result, and post-deploy smoke.
6. Range plane: provider quota, subnet/NAT capacity, active/stuck operations,
   cleanup verification, residual inventory, application/model budgets, billing
   budget alerts, and approved event-capacity limits.

Read-only command:

```bash
kubectl -n "$NAMESPACE" get deploy,pods
kubectl -n "$JOBS_NAMESPACE" get jobs \
  -l app.kubernetes.io/component=pulumi-provisioner
kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp
```

## Resolution

Open the narrow runbook for the failed signal. Restore the owning dependency or
redeploy an exact verified release through the protected workflow. A healthy
process, HTTP response, Terraform apply, or smoke check proves only its own
boundary; keep the incident open until the failed user/operator path and
independent cleanup or containment evidence recover.

## Prevention

Maintain a dashboard with the independent signals above, synthetic public and
lifecycle probes, capacity/cost headroom, and release identity. Run the
[functional smoke](../qa/range-functional-smoke.md) for release qualification,
not as a replacement for continuous health checks.

Escalate on cross-layer disagreement, unknown running artifacts, containment
signal loss, exhausted quota/budget, or provider inventory without ownership.
