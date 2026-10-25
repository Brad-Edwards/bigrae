# RB020: Provisioner timeout

Applicability: Kubernetes provisioner Jobs on GCP/AWS. Operator: range and
platform owner. Verified: 2026-10-23.

## Symptom

A range operation exceeds its expected duration, a provisioner Job reaches its
deadline, or the launcher repeatedly waits for a provider result.

## Investigation

1. Record request ID, operation ID/generation, operation type, launch-intent
   state, Job start/deadline, last bounded log timestamp, and provider effects.
2. Determine where time is spent: queued launch, Kubernetes scheduling/image
   pull, policy admission, Terraform/Pulumi, provider quota/throttling, image
   readiness, guest readiness, result append, result application, or event
   delivery.
3. Check cluster capacity, Job events, provider operation status, worker
   heartbeats, result inbox age, and cleanup state. Confirm whether cancellation
   or interrupt has already been accepted.
4. Legacy alerts may say Lambda timeout; range operations are Jobs, and raising
   a Lambda timeout cannot repair them.

## Resolution

Restore the failed dependency or capacity through its owner. Use the authorized
cancel/retry lifecycle service only after classifying the original operation
and preserving its generation/identity. Never relaunch a Job directly or raise
timeouts blindly; an active provider effect can make a duplicate launch unsafe.
Verify result application, lifecycle convergence, cleanup verification, and
provider inventory.

## Prevention

Measure each stage independently, set operation-specific SLOs and bounded
deadlines, alert on oldest launch/result age, retain provider request IDs, and
capacity-plan Jobs, quotas, images, NAT, and guest readiness.

Escalate past the operation lease, on uncertain provider completion, after a
cancellation race, or when the safe retry action is unavailable.
