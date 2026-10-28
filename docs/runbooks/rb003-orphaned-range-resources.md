# RB003: Orphaned range resources

Applicability: GCP/GCE observation; AWS observation with a limited legacy tool.
Operator: platform and range owner. Verified: 2026-10-23.

## Symptom

Provider resources carry Shifter ownership labels but no active range owns
them, or a terminal range still has instances, disks, networks, or secrets.

## Investigation

1. Freeze mutation for the affected range and export a redacted inventory keyed
   by request ID, operation ID, generation, resource type, and ownership labels.
2. Compare provider inventory with Engine ranges, immutable operation inputs and
   results, launch intents, cleanup verification, and provisioner state. Do not
   use the CMS projection alone as lifecycle authority.
3. On GCP, query resource types separately with purpose-scoped `gcloud` list or
   describe commands; do not broaden IAM or enable APIs during an incident just
   to build an aggregate inventory.
4. On AWS, `shifter-ops` can classify EC2 candidates, but its current
   `reconcile_ranges execute` path terminates instances before non-transactional
   direct SQL updates. Treat it as unqualified for recovery; use observation
   output only.

## Resolution

There is no approved provider-neutral orphan mutation. Preserve evidence,
quarantine affected networks and credentials through their existing owners,
and escalate for a reviewed cleanup plan that names every resource, durable
owner, order, rollback boundary, and independent readback. Never delete by name
prefix, edit status rows, or remove Terraform state to make inventory appear
clean.

## Prevention

Require ownership labels and operation IDs on every provider effect, make
cleanup verification part of lifecycle completion, and run scheduled residual
inventory comparisons. A successful destroy must prove both durable convergence
and absent provider resources.

Escalate every confirmed orphan; this runbook intentionally has no direct
delete command.
