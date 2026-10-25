# RB010: Agent upload failed

Applicability: GCS primary; S3 optional through the shared object-storage
contract. Operator: CMS/application owner. Verified: 2026-10-23.

## Symptom

An agent upload cannot start or finalize, remains pending, fails validation, or
the installed object cannot be found.

## Investigation

1. Record the upload ID and sanitized request ID. Do not record the signed URL,
   token, object key if tenant-sensitive, or object contents.
2. Determine whether failure occurred at admission, signed-upload transfer,
   object `HEAD`, size/header validation, immutable finalization, installation,
   or cleanup reconciliation.
3. Check application logs, upload state, scoped quota reservation, object
   metadata, bucket availability, IAM/service identity, CORS, and provider
   status. Use the `ObjectStorage` boundary; compatibility module names do not
   make a GCP upload an S3 workflow.
4. Confirm the object belongs to the same authorized scope and upload record
   before any cleanup.

## Resolution

Retry admission or finalization through the CMS upload service after correcting
the classified failure. Retire an invalid or superseded upload through the
owning service and allow its cleanup reconciliation to delete the object. Never
upload directly with operator credentials, move objects between scopes, change
database byte counts, or delete by guessed prefix. Verify immutable installed
identity, audit, quota release, and absence of abandoned pending reservations.

## Prevention

Monitor admission/finalization failures, pending age, cleanup backlog, object
storage availability, and quota headroom. Keep size and header validation on
the server and test provider adapters with bounded synthetic objects.

Escalate ownership mismatch, repeated signed-upload failure, missing object
custody, or any request to inspect uploaded content outside the authorized
scope.
