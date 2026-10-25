# RB022: AWS Firehose delivery lag

Applicability: AWS only. GCP uses Cloud Logging and this runbook does not apply.
Operator: AWS platform/security owner. Verified: 2026-10-23.

## Symptom

The Firehose `DeliveryToS3.DataFreshness` alarm fires or CloudWatch log records
arrive late in the encrypted log bucket.

## Investigation

1. Confirm the selected AWS environment, stream, source log subscriptions,
   incident window, and alarm state. The current threshold is 900 seconds; the
   delivery stream uses a five-minute buffer.
2. Inspect Firehose delivery metrics, error log, S3 error prefix, destination
   bucket/KMS health, quota/throttling, and IAM policy. Preserve bounded redacted
   errors.
3. Confirm object arrival separately from the downstream SQS notification
   consumer and any external SIEM ingestion. These are three different proofs.
4. Check recent Terraform/deployment changes. Do not use the GCP Pub/Sub
   lifecycle DLQ or Cloud Logging procedures for this AWS data path.

## Resolution

Restore the Terraform-owned source subscription, Firehose destination, KMS/IAM,
or bucket dependency through the protected AWS deployment path. Replay from a
retained source only with security-owner approval and a duplicate-handling plan.
Do not make the bucket public, disable encryption, widen IAM, or delete the
error prefix. Verify freshness recovery, object arrival, downstream
notification, and consumer acknowledgement independently.

## Prevention

Alert on freshness, delivery failure, error-prefix growth, throttling, and
downstream oldest-message age. Test KMS/IAM changes before rollout and retain
buffer/capacity headroom.

Escalate when source records may be lost, encryption or bucket policy fails, a
replay could duplicate security events, or downstream ingestion remains stale
after S3 delivery recovers.
