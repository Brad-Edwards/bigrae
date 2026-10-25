# RB008: Dead-letter messages

Applicability: GCP Pub/Sub lifecycle events primary; AWS SQS queues optional.
Operator: platform owner. Verified: 2026-10-23.

## Symptom

The lifecycle dead-letter subscription grows, an event consumer falls behind,
or an AWS notification DLQ alarm fires.

## Investigation

1. Identify the queue contract and consumer. GCP Pub/Sub range lifecycle events
   serve CMS, Engine, and Mission Control consumers; they are not log-object
   notifications.
2. Inspect topic/subscription configuration and backlog without consuming or
   acknowledging messages:

   Read-only command:

   ```bash
   gcloud pubsub subscriptions list --project "$PROJECT_ID" \
     --format='table(name.basename(),topic.basename(),deadLetterPolicy.deadLetterTopic.basename(),ackDeadlineSeconds)'
   ```

   Use Cloud Monitoring for backlog size and oldest-message age; subscription
   configuration does not report those live metrics.

3. Check the matching worker's heartbeat and bounded logs. Classify messages as
   poison (schema/validation), permanent authorization/configuration failure, or
   transient dependency outage.
4. Confirm the durable application outbox/inbox state and idempotency key before
   considering redelivery. Queue depth alone is not lifecycle authority.
5. On AWS, distinguish application task DLQs, range-event delivery, and the
   log-notification SQS DLQ before using an AWS-specific procedure.

## Resolution

Fix the consumer or dependency first. Replay only through an approved,
contract-aware operator surface after proving the handler is idempotent and the
message remains applicable. The repository does not expose a general GCP bulk
replay command; preserve the subscription and escalate rather than pulling and
republishing messages by hand. Verify backlog decline, consumer health, and
domain-state convergence.

## Prevention

Alert on oldest-message age as well as depth, retain dead-letter reason metrics,
test schema compatibility, and keep consumers safe under at-least-once
delivery. Use bounded retry and exponential backoff.

Escalate for poison messages, an absent replay surface, authorization failure,
or any proposed bulk acknowledgement/deletion.
