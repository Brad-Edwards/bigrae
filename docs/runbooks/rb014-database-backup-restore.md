# RB014: Database backup, restore, and reconciliation

Applicability: Cloud SQL primary; RDS optional. Operator: data owner, platform
owner, and incident commander. Verified: 2026-10-23.

## Symptom

Durable application state must be restored after loss or corruption, or backup
recoverability needs qualification.

## Investigation

1. Declare the recovery point and objective, incident scope, selected backup,
   database engine/version, encryption/key references, and all dependent duties:
   application, Guacamole, OpenFGA, and provisioner access.
2. Record the restore inventory: database backup, required objects,
   configuration revision, secret/key references, provider resource IDs,
   immutable operation inputs/results, outboxes, and cleanup verification.
3. Disable new range admission and conflicting mutations through approved
   controls. Preserve the source backup and current provider inventory.
4. Validate an isolated restore where supported. Check schema/migration level,
   identity bindings, object references, unknown operations, residual ranges,
   queue cursors, and Terraform ownership before considering cutover.
5. GCP automated backups are enabled, but the repository does not yet provide a
   fully qualified one-command point-in-time restore/cutover procedure.

## Resolution

Restore to a clean owned database under the data owner's approved provider
procedure. Keep admission and mutation disabled while reconciling restored
domain state against live provider inventory. Unknown operations and residual
resources stay blocked and visible. After reconciliation, verify one restored
range can be safely inspected or cleaned up, then launch and destroy one bounded
synthetic range before reopening admission. Record observed RPO/RTO and gaps.

## Prevention

Keep automated backups and retention monitored, version required object stores,
retain operation history, and rehearse isolated restoration for each supported
release. A backup-success metric is not restore evidence.

Escalate every production cutover, ambiguous provider ownership, missing key or
object dependency, schema mismatch, or failed cleanup proof.
