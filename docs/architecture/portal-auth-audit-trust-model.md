# Portal Auth & Audit Trust Model

Status: implemented on the #2321 branch; includes the #2617 portal security controls.

This document separates login reachability, identity evidence, authorization,
and audit attribution. The native principal and policy boundary in
[ADR-066](../adr/066-account-principal-resource-scope.md) is authoritative.
Legacy Django groups, staff flags, email matches and profile claims cannot grant
native authority. The [lifecycle contracts](../technical/shifter_platform/identity-lifecycle-contracts.md)
define command ownership, proof checks, transactions and recovery.

## Dev-auth reachability

`/dev-login/` and `/dev-logout/` are unauthenticated by design and gated in two
layers (`config/dev_auth.py`):

1. **Hard environment gate** (`_is_dev_environment`): admits only when
   `DEBUG=True` or `ENVIRONMENT == "development"`. Every other environment
   returns 403 before any user lookup, login, or group mutation. Deployed dev
   and prod export `ENVIRONMENT=production`, so this is fail-closed.
2. **Direct-peer admission** (`_request_peer_allowed`): when `DEBUG` is False,
   admission is bound to the actual socket peer (`REMOTE_ADDR`): the loopback
   range is always admitted (local dev and SSM/admin tunnels present as
   loopback) plus any `DEV_LOGIN_ALLOWED_CIDRS`. The spoofable `Host` header and
   `X-Forwarded-For` are never consulted for this decision. Django host
   validation (`ALLOWED_HOSTS`) protects URL construction only; it is not a
   dev-auth allowlist.

## Audit source-IP attribution

`shared.audit.get_client_ip()` is the single canonical resolver for
HTTP audit `source_ip`. It delegates to `select_trusted_client_ip()`, which
trusts the **rightmost** (proxy-appended) `X-Forwarded-For` hop (the value the
ALB appends is the real client as seen by the trusted proxy), and falls back to
`REMOTE_ADDR` when the chain is absent, shorter than the trusted hop count, or
the selected token is not a valid IP. The client-controlled leftmost value is
never trusted. The number of trusted proxy hops is configured by
`AUDIT_TRUSTED_PROXY_HOPS` (default 1, modelling the single AWS ALB). On GCP,
`scripts/gcp/render_runtime_env.py` sets it to 2: the GCE ingress external
Application Load Balancer appends `<client-ip>,<load-balancer-ip>`, so the
client is the second hop from the right. The WebSocket
terminal consumer (`mission_control/consumers.py`) reuses the same hop policy on
the ASGI scope so terminal audit rows do not drift from HTTP rows.

## Identity evidence and self-service claims

Provider login binds a verified issuer and subject to a native human principal.
Email and display names are profile data. A self-service `user_type` claim may
update compatibility profile/group projections through `config.user_type_sync`.
It cannot grant native organizer, content-authoring or installation authority.
The participant projection also cannot substitute for current event standing.

Provider groups and legacy bootstrap-email settings do not confer native
permissions. Installation bootstrap uses the explicit native operator path and
exact configured issuer/subject binding. Administrators manage subsequent
relationships through the native policy commands. Retained compatibility
modules and historical migrations are not instructions for current grants.

## Organizer and administrative authority

Each HTTP, socket and service boundary retains the caller's original native
credential. The owning service resolves current proof and evaluates the exact
action, target and scope. An event owner field or legacy `CTF Organizer` group
cannot bypass that decision. Revoking policy denies later campaign reads,
scheduled release, notification delivery and socket admission even when the
same session is still valid. Participant access also checks current event
standing, including suspension and the event window.

Staff assignment names a native principal UUID. Email-only requests are rejected
without a directory lookup; known and unknown email addresses have the same
response. Event staff changes use the retained native policy transition and
audit protocol.

`/admin/login/` redirects safe requests to the configured provider login.
Password submissions to that route are refused. The mounted `PolicyAdminSite`
permits only policy-authorized inspection. It provides no identity, password,
group or privilege writes, including for a legacy Django superuser. Native APIs
and their owning services are the mutation boundary. Password-reset delivery
rejects console and file mail backends before minting a reset link.

## Audit identity and mutation records

Native audit records retain the principal UUID for both human and service
callers. HTTP audit context uses `shared.audit.request_audit`; integer user IDs
may supplement that context but cannot replace the native principal.
Client-supplied request IDs are length- and character-bounded before persistence.
Malformed or rejected token requests are recorded without token secrets.
The source-IP rules above apply to both HTTP and socket attribution.

Organizer range commands retain the initiating credential and record intent and
outcome. Cleanup admits policy before entering the mutation/audit transaction;
provider calls do not run under that transaction's SQL locks. Public failures
use the bounded error envelope, without internal exception details.

The Engine image-registry service records the principal and the exact previous
and new boot-image selection in the same transaction as the mutation. Its audit
is strict: a failed audit rolls back the change. The API does not add a duplicate
best-effort record or read an unscoped registry to obtain the previous state.
Legacy role-sync projections retain their strict transactional audit, but an
audited projection is still not an authorization grant. Audit records exclude
tokens, cookies, full headers and provider payloads.
