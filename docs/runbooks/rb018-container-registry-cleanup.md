# RB018: Container registry cleanup

Applicability: GCP Artifact Registry primary; AWS ECR optional. Operator:
release/platform owner. Verified: 2026-10-23.

## Symptom

Registry storage cost or image count grows, cleanup policy fails, or old images
appear eligible for deletion.

## Investigation

1. Inventory repositories, digests, tags, creation/download time, size, and
   retention policy. Work only from digests; tags are mutable references.
2. Build a protected set from running portal/worker/provisioner/Guacamole
   workloads, current and rollback releases, release evidence, provisioner
   inputs, and image-promotion channels.
3. On AWS, Terraform lifecycle policy retains the configured recent image set.
   Confirm policy status and exclusions before manual action.
4. GCP Artifact Registry currently has no repository-owned cleanup policy. A
   list of old images is evidence, not authorization to delete them.

## Resolution

For AWS, repair and apply the Terraform-owned lifecycle policy. For GCP,
preserve the inventory and escalate a reviewed cleanup-policy change; do not
perform ad hoc deletion from this runbook. Any approved emergency deletion must
use exact digests, exclude the protected set, be bounded and logged, and verify
that running/rollback images still resolve.

## Prevention

Add retention as provider-owned Terraform with dry-run reporting, explicit
protected release classes, cost thresholds, and rollback tests. Track registry
growth and unreferenced digests before the budget limit is reached.

Escalate every GCP deletion until a qualified automated policy exists, or when
provenance cannot establish whether a digest is still required.
