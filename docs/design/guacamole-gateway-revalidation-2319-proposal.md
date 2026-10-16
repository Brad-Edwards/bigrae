# Guacamole gateway revalidation for #2319

Status: approved by the user on 2026-10-14; implemented and locally qualified.
Production activation remains S8/#2321. Qualification evidence and fixture
bounds are recorded in the technical content/range authority guide.

## Confirmed gap

The existing portal controller can invalidate a reachable Guacamole session
through native `DELETE /api/session`. Native synthetic SSH/SFTP qualification
already demonstrates guest transport closure through that path. A lost portal
controller or an unreachable Guacamole control API can leave an active tunnel
alive, so this path does not yet satisfy #2319's dependency-failure bound.

This is established by the pinned Apache 1.6.0 implementation:

- [HashTokenSessionMap](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/auth/HashTokenSessionMap.java)
  explicitly excludes sessions with active tunnels from inactivity expiry.
- [The JSON UserContext](https://github.com/apache/guacamole-client/blob/1.6.0/extensions/guacamole-auth-json/src/main/java/org/apache/guacamole/auth/json/user/UserContext.java)
  exposes the supplied connection directory without implementing continuing
  application authority or expiry invalidation.
- [JSON authentication](https://guacamole.apache.org/doc/gug/json-auth.html)
  limits admission through the signed payload's expiry; it does not provide a
  portal-independent application revocation check for an established tunnel.

Reducing the inactivity timeout or JSON admission lifetime therefore does not
close this gap. RDP still needs its own native transport qualification.

## Recommended change

Use Guacamole's supported extension API to decorate the authenticated user
context and invalidate that context when its original Shifter authority is
revoked, changes scope, or cannot be renewed within a bounded interval. Retain
JSON authentication and native session invalidation; add no alternate login,
credential, cryptography or policy evaluator.

The extension binds an opaque bootstrap identity to the existing recorded
principal, credential reference, ceiling, resource generation and scope. It
queries the owning portal service through a workload-authenticated control
endpoint, using the established provider identity mechanism. The portal applies
the existing common policy to the original proof. The extension receives only a
bounded authority result and retains no application bearer token.

The gateway enforces the last successful renewal's deadline locally. A control
outage cannot indefinitely extend it. Invalidating the context uses the native
[session invalidation implementation](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/GuacamoleSession.java),
which closes associated tunnels. The native eviction task checks context
validity even while tunnels are active. Its one-minute scheduling interval must
be included in qualification, along with native guest shutdown time.

## Approval boundary and verification

The approved C4 plan selected the existing portal-side invalidation seam. This
proposal adds a gateway extension, its workload binding and a control-service
integration. The user approved that additional architecture boundary on 2026-10-14.

Qualification must measure actual SSH, RDP and SFTP guest closure under normal
revocation, missing notifications, evaluator failure, loss of the portal
controller, unreachable control API, renewal timeout and concurrent renewal.
Load tests must cover the gateway timer and bounded renewal queue. No production
numerical guarantee is established by this proposal.

The remaining authorized C4 service integration can continue independently.
