# RB009: User locked out or credential revoked

Applicability: Identity Platform primary; Cognito/OIDC and local accounts where
configured. Operator: authorized organization administrator. Verified:
2026-10-23.

## Symptom

A user cannot authenticate, an account is suspended, MFA or provider recovery
fails, or an incident requires credential revocation.

## Investigation

1. Identify the account origin: Identity Platform, Cognito/OIDC, eligible local
   account, service credential, or temporary CTF account. Do not infer origin
   from email alone.
2. Correlate sanitized authentication/audit events and request IDs. Check
   provider status, allow-list policy, application activation state, workspace
   membership, and credential admission separately.
3. Confirm the requesting administrator's organization/resource scope and the
   affected user's identity before reset, suspension, deactivation, or
   revocation.
4. For a range-security incident, determine whether accepted cleanup authority
   must outlive caller credential revocation before changing access.

## Resolution

Use provider-native recovery for provider accounts. Use the application
administrator password-reset service only for eligible local accounts. The
service needs a configured email provider. With the console or file email
backend, it refuses the request (`reset_delivery_unavailable`), because those
backends write the reset link to the logs. Use
`management.lifecycle` and the credential service for application suspension,
deactivation, or revocation; do not edit authentication rows or MFA material.
After revocation, verify new sessions fail, existing access is invalidated as
designed, audit records exist, and any accepted range cleanup can still finish.

## Prevention

Maintain a tested administrator recovery path, two independent authorized
administrators, provider status monitoring, scoped audit review, and periodic
credential inventory. Keep service and human credentials distinct.

Escalate suspected compromise, identity-origin ambiguity, cross-organization
impact, missing audit evidence, or cleanup that would be stranded by revocation.
