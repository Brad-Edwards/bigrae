# Workspace member invitations

Issue #1942 implements PLAT-235 within the ADR-046 workspace boundary. The
architecture binding is recorded in
[`workspace-member-invitations-preflight-1942.md`](../../architecture/workspace-member-invitations-preflight-1942.md).

`WorkspaceInvitation` is a pre-membership grant owned by `workspaces`. It stores
the normalized recipient email, closed role, expiry, revocation/acceptance state,
and a rotating generation UUID. A conditional case-insensitive database
constraint permits only one current invitation per workspace/email. Membership
creation still goes through the canonical locked membership insertion service.

The email carries a Django timestamp-signed payload in the URL fragment. The
public landing immediately removes the fragment from browser history and sends
the token to an exact CSRF-protected same-origin staging endpoint. Successful
staging stores only invitation UUID and generation in the session. Raw tokens
are not logged, persisted, rendered, or returned by the administration API.

Acceptance is coupled to the normal provider login transaction. Both supported
identity adapters attach a fresh `VerifiedIdentity` to the request; a
`user_logged_in` receiver consumes the staged grant using that evidence. A
pre-existing Django session is forced through provider reauthentication. The
recipient email must match, temporary CTF accounts and ambiguous active account
matches fail closed, and an existing membership is never altered.

The administration API is browser-session-only and requires both `is_staff` and
the relevant live workspace operation. Owner and admin roles may manage member
and admin invitations; owner grants require owner authority. Issue/resend use
the shared credential-delivery limiter and shared email adapter, and every
lifecycle mutation writes strict invitation audit records without recipient PII
or signed credentials.

Delivery follows ADR-071. Issue and resend commit the invitation and audit
first. An `on_commit` callback then makes one bounded `shared.email.attempt_email()`
call. It sends only when the invitation is current and its generation is
unchanged. It stores `delivery_status`, `delivery_reason`, and
`delivery_observed_at` with a compare on that generation, so a later resend or
revoke fences an earlier send. The status is `pending`, `accepted`, `failed`,
`unavailable`, or `unknown` (rows created before #1525). `unavailable` means no
delivering backend is configured, and nothing was sent. Resend is the retry.
There is no outbox, retry loop, or thread pool. See the
[email delivery design](../../architecture/email-delivery-design-1525.md).
