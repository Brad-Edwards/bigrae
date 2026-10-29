# ADR-071: Email delivery reports observed outcomes

## Status

Accepted 2026-10-29. Specializes ADR-051 for the CTF email channel (ADR-051-R14)
and amends PLAT-103. Issue
[#1525](https://github.com/Brad-Edwards/bigrae/issues/1525) records the work.
The [design](../architecture/email-delivery-design-1525.md) records the
architecture, the data flows, and the security controls.

## Context

CTF announcement state advanced before recipient outcomes were known. Workspace
invitations used a fire-and-forget thread pool, so a restart lost the send.
Administrator password reset reported success when the send failed. A console
backend printed signed links to logs. The issue review (#2080) forbids a second
email outbox. It requires retries, idempotence, and observable failure for each
claimed critical delivery.

## Decision

1. **CTF email uses the communication ledger.** Email is a `ChannelAdapter`.
   The `DeliveryAttempt` row is the durable command. The existing worker owns
   retry, backoff, and the attempt and elapsed ceilings. It checks the ceilings
   before I/O as well as after a result.
2. **The occurrence delivery state is persisted.** `CommunicationIntent`
   stores `delivery_state` separately from its release lifecycle. One locked
   derivation from committed rows writes it.
3. **Invitation and reset messages use one bounded synchronous attempt after
   commit.** The invitation stores its delivery status for the current
   generation. The reset API returns `reset_delivery_failed` when the backend
   does not accept the message. Resend is the retry. No second outbox exists.
4. **`shared.email.attempt_email()` is the only transport seam.** It makes one
   backend call within one wall-clock deadline. Each blocking read and write of
   the connection gets only the time left. It returns a closed outcome, a closed
   reason, and an optional validated receipt. SDK retries and django-ses
   automatic throttling are off.
5. **Non-delivering backends are unavailable outside development and test.**
   A console, file, or dummy backend sends nothing in a deployed runtime. The
   outcome is `unavailable`, never accepted.
6. **Idempotency is at the ledger level.** One command exists for each recipient
   and channel. Each attempt carries a stable Message-ID from the command key.
   A crash after provider acceptance and before settlement can send a duplicate.
   This at-least-once window is a documented limit.

## Consequences

- Provider acceptance is the strongest success claim. It is not mailbox
  delivery, read, or acknowledgement.
- An HTTP request that sends an invitation or reset waits up to the interactive
  timeout.
- Deployments that render a console backend report email as unavailable.
- Organizer-only alerts stay unavailable. The participant ledger has no
  organizer recipient contract (ADR-065).
