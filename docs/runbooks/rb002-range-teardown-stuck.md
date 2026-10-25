# RB002: Range teardown stuck

Applicability: GCP/GCE primary; AWS optional. Operator: range and security
operator. Verified: 2026-10-23.

## Symptom

A range remains destroying, a cancel or expiry does not converge, or the portal
shows destroyed while provider resources are still present.

## Investigation

1. Record request ID, destroy operation ID, generation, persisted backend
   binding, cancellation state, lease, and cleanup-verification state.
2. Inspect `worker-provisioner-launcher`, provisioner Jobs, the operation-result
   applier, and `worker-reconciler` for the bounded incident window.
3. Inventory GCE resources by ownership labels. Include instances, disks,
   subnets, routes/NAT enrollment, addresses, firewall bindings, and dynamic
   secrets. For AWS, include EC2, ENIs, volumes, security groups, routes, and
   provisioner state.
4. Treat a logical `destroyed` status and a successful Job exit as separate
   evidence from an empty provider inventory.

## Resolution

If the application still offers cancel or destroy, invoke it once through the
authorized lifecycle service and follow the resulting operation ID. Do not
delete provider resources first: that breaks the operation-result and cleanup
proof. If a credential must be revoked to contain an active range, use the
approved credential/lifecycle service, preserve the accepted cleanup authority,
and confirm that the destroy intent remains executable after caller revocation.

When the owning operation is terminal but residuals remain, block reuse of its
network and identifiers and escalate to the platform owner. GCP has no approved
general orphan-mutation command. Verify completion only after cleanup
verification and independent provider inventory agree.

## Prevention

Monitor destroy-operation age, interrupt backlog, cleanup-verification outcome,
lease expiry, and residual inventory. Preserve the write-once backend binding so
selector changes cannot route cleanup through the wrong provider.

Escalate on ambiguous backend ownership, a revoked authority that blocks
accepted cleanup, network isolation failure, or any proposed direct cloud
deletion.
