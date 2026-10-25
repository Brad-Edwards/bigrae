# RB007: Logs or containment signals not flowing

Applicability: GCP Cloud Logging primary; AWS log aggregation optional.
Operator: security and platform owner. Verified: 2026-10-23.

## Symptom

Application, audit, or range-containment records stop arriving at the expected
controlled log destination, or an external collector reports a gap.

## Investigation

1. Define the missing signal precisely: platform application log, audit event,
   range-escape validation record, GCE serial/system log, or downstream SIEM
   ingestion. These have different owners.
2. Query Cloud Logging for a bounded interval using deployment labels and a
   sanitized request/operation ID. Confirm source emission before investigating
   a sink or collector.
3. Check logging exclusions, quotas, sink/collector status, IAM, and recent
   configuration changes. Object arrival in GCS does not prove downstream SIEM
   ingestion.
4. The first GCP release provides structured Cloud Logging containment signals;
   it does not provide continuous guest host sensors or a repository-owned XDR
   export sink. Treat those as capability gaps, not failed components.
5. On AWS, follow the Terraform-owned CloudWatch subscription, Firehose,
   encrypted S3, and notification queue independently from application
   lifecycle queues.

## Resolution

Restore the source workload or Terraform-owned logging path through its owning
deployment. If containment visibility is lost, pause new range admission and
quarantine affected ranges through the authorized lifecycle/security process
until an independent signal is available. Do not broaden log-reader IAM, disable
redaction, or paste raw provider logs into public tickets.

## Prevention

Use synthetic canaries for source-to-destination freshness, alert on logging
quota/exclusion changes, and keep collector qualification separate from object
delivery. See [GCP range audit telemetry](../ops/gcp-range-audit-telemetry.md)
and [range escape validation](../ops/range-escape-validation.md).

Escalate when containment signals are unavailable during live use, evidence may
have been lost, or the requested collector is not part of the released system.
