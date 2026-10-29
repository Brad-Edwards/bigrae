# RB006: Worker or provider-function errors

Applicability: Kubernetes workers and Jobs on GCP/AWS; specific AWS functions
only when deployed. Operator: platform owner. Verified: 2026-10-23.

## Symptom

A worker repeatedly fails, a heartbeat probe is unhealthy, or an optional cloud
function reports errors. Legacy alerts may still call this a Lambda failure.

## Investigation

1. Identify the exact component and duty. Range provisioning is handled by
   `worker-provisioner-launcher` and Kubernetes provisioner Jobs, with separate
   result-applier, outbox, and reconciler workers.
2. Check deployment availability, restarts, heartbeat probes, recent events,
   and bounded logs:

   Read-only command:

   ```bash
   kubectl -n "$NAMESPACE" get deploy \
     -l app.kubernetes.io/part-of=shifter
   kubectl -n "$NAMESPACE" get pods \
     -l app.kubernetes.io/part-of=shifter
   ```

3. Correlate failures with request/operation IDs and classify configuration,
   database, queue, provider, quota, policy-admission, image, or code failure.
4. On AWS, inspect a Lambda only after confirming the named function is part of
   the selected deployment; identity rotation functions are separate from
   range provisioning.

CTF status deliveries use the existing `worker-reconciler`. A receiver failure
leaves the CMS status committed and retains a `cms.RangeStatusDelivery` row.
The row contains only the source id, status transition, verified-cleanup flag,
attempt count, next attempt time, and exception class. Retry proceeds in status
order for each range, with backoff capped at five minutes; a failing range does
not block other ranges. Successful receiver writes and deletion of the pending
row commit together. Pending rows survive terminal CMS states and source-row
pruning. Correlate `CTF range status delivery retained for retry` records by
delivery id and range-instance id; `ctf_delivered` in the reconciler summary
shows recovery. Restore the failed dependency and let the reconciler retry;
do not delete pending rows to clear an error.

## Resolution

Restore the failed dependency first. Redeploy a worker or roll back its exact
image through the protected workflow, preserving service account and runtime
configuration. Do not run a worker command from a web pod or grant a workload a
different identity. For a provisioner failure, continue with
[RB001](rb001-range-provisioning-failed.md) so provider effects are reconciled.

## Prevention

Alert on heartbeat freshness, restart rate, queue age, Job failure, result age,
and provider throttling separately. Keep workload duties and identities split
and retain the deployment readback.

Escalate on repeated crash loops after rollback, admission-policy rejection,
unknown image identity, or any proposal to bypass the worker boundary.
