# RB011: Upload storage quota exceeded

Applicability: application quota on GCS or S3. Operator: CMS/application owner.
Verified: 2026-10-23.

## Symptom

An upload is refused because the user's storage budget or per-file limit is
exceeded, even though the cloud bucket itself has capacity.

## Investigation

1. Confirm whether the rejection is the per-file limit or the user storage
   quota. The enforced settings are `AGENT_MAX_FILE_SIZE_MB` and
   `AGENT_USER_STORAGE_QUOTA_MB`.
2. Inspect installed agents, pending uploads, scoped reservations, failed
   finalizations, and cleanup backlog through the application service. Include
   pending reservations in the total.
3. Check recent configuration and deployment readback. Do not treat provider
   bucket capacity, billing quota, or lifecycle-retained objects as the same
   limit.
4. Verify every candidate retirement belongs to the affected scope and is not
   referenced by an active deployment or range.

## Resolution

Have the authorized owner retire obsolete agents through the CMS service and
allow the cleanup path to reconcile storage and reservations. Increase a quota
only as a reviewed configuration change with capacity and cost approval, then
deploy through the protected workflow. Do not delete bucket objects or edit
budget counters directly. Verify the recalculated budget and a bounded upload.

## Prevention

Expose quota usage and pending reservations to administrators, alert on cleanup
backlog and sustained headroom loss, and review limits with object-storage cost
budgets. Use retention policy rather than emergency deletion.

Escalate when usage cannot be reconciled to owned records, cleanup is stuck, or
the requested increase exceeds the deployment's approved cost limit.
