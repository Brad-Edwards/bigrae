# RB017: Stale range cleanup

Applicability: provider-neutral CMS/Engine reconciliation; GCE primary.
Operator: range/platform owner. Verified: 2026-10-23.

## Symptom

A cancelled or expired range remains active, CMS status lags authoritative
Engine state, or range events appear lost.

## Investigation

1. Record request ID, Engine and CMS states, operation generation, lease,
   interrupt/cancellation state, launch intent, outbox status, and cleanup
   verification.
2. Check `worker-reconciler`, `worker-provisioner-launcher`,
   `worker-operation-result-applier`, and `worker-outbox-drainer` availability
   and heartbeat. Correlate bounded sanitized logs.
3. Determine whether the problem is a stale CMS projection, pending lifecycle
   event, due interrupt, active provider operation, or residual cloud resource.
4. `reconcile_range_events` converges allowed CMS projections and invokes the
   canonical lease-expiry path. It is not a general orphan-resource deleter.

## Resolution

Restore the deployed reconciler and its dependencies through the protected
workflow. A one-shot command may be used only through an approved operator
transport with the existing worker identity, bounds, locks, and audit; the
repository does not currently publish a provider-neutral ad hoc production
transport. Do not use `kubectl exec`, direct SQL, or hand-built events. Verify
Engine/CMS convergence, accepted cleanup operation, cleanup verification, and
independent provider inventory.

## Prevention

Alert on reconciler heartbeat, projection age, expired leases, interrupt age,
outbox age, and cleanup-verification failure. Preserve row locks, generation
fences, and at-least-once event idempotency.

Escalate when the worker is healthy but states cannot converge, a terminal state
would need reversal, or provider residuals remain.
