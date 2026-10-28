# VPN credential binding proposal for #2319

Status: approved by the user on 2026-10-14: "Agreed. Implement inline with the
official guidance." This authorizes the revised certificate issuance, CRL and
native management integration within #2319. Production activation remains S8.
Reference comparison updated on 2026-10-14 against OpenVPN Community 2.6,
the version family installed by the existing pool image.

The current VPN implementation creates one certificate and profile per range
generation. Its certificate common name contains only the generation UUID.
Profile downloads return the same secret material to every authorized caller.
The pool controller reports the common name, server and client ID; it does not
report a certificate identity that distinguishes those callers.

Relevant existing owners are `shifter/engine/provisioner/vpn_access.py`,
`vpn_secrets.py`, `shifter_openvpn/controller.py`, and Engine's `_vpn.py` and
`_vpn_sessions.py`. Only the provisioner reads the existing tenant CA signing
key. The portal's profile service reads participant material.

## OpenVPN reference architecture and the existing implementation

This extension must use OpenVPN's established client PKI and management
interface. It does not require a new VPN authentication or revocation protocol.

| Concern | Authoritative OpenVPN guidance | Existing Shifter implementation and proposal |
| --- | --- | --- |
| Client identity | Give each client its own certificate/key and unique common name. See the [client PKI guide](https://openvpn.net/community-docs/setting-up-your-own-certificate-authority--ca--and-generating-certificates-and-keys-for-an-openvpn-server-and-multiple-clients.html). | The provisioner gives a range generation one identity, shared by every permitted download. That identifies the range, but cannot identify the accessing credential. Issue an individually bound profile with an opaque, unique common name. |
| Signing authority | Keep the CA private key on the signing machine; VPN servers need the CA certificate. See the same [PKI guide](https://openvpn.net/community-docs/setting-up-your-own-certificate-authority--ca--and-generating-certificates-and-keys-for-an-openvpn-server-and-multiple-clients.html). | Preserve the provisioner's existing signing boundary and provider secret controls. Neither the portal nor the pool receives the CA private key. This is an existing online issuer, not an offline CA. |
| Application authorization | Use `management-client-auth` after certificate verification. See the [2.6 manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html). | Already configured. Extend the existing controller/service decision with the immutable profile binding and current Shifter policy. Keep the local Unix socket and its existing user restriction. |
| Certificate revocation | Publish a CA-signed CRL and configure `crl-verify`. See the [revocation guide](https://openvpn.net/community-docs/revoking-certificates.html). | The current server configuration has no CRL directive, and the earlier proposal omitted this lifecycle. Add durable certificate revocation and CRL distribution to the existing pool lifecycle. Deleting a profile secret does not revoke a delivered certificate. |
| Active connection termination | Use `client-kill` for immediate termination; denial during renegotiation can leave the old key active. See the [management interface reference](https://openvpn.net/community-docs/management-interface.html). | The controller already uses native `client-kill`. Retain it, remove forwarding allowances, and qualify actual tunnel termination. A CRL update alone does not immediately end an established tunnel. |

These references establish the mechanisms, not Shifter's identity granularity.
OpenVPN does not require one certificate per browser session. The older PKI
HOWTO also contains obsolete command and cryptographic examples; reuse its
ownership pattern with the installed 2.6 configuration and existing X.509
library, not those historical parameters.

## Which credential the certificate represents

A certificate per user or device is a conventional alternative. By itself,
however, it does not distinguish two portal credentials belonging to the same
principal. #2319 requires the original accessing credential and its original
ceiling to remain authoritative; another login or download must not replace it.

OpenVPN also supports certificate authentication combined with username/password
authentication through its native hooks, as described in the
[2.6 manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html).
That could keep a device certificate stable, but still needs an independently
verified application credential at each connection. Merely supplying a principal
or credential UUID is not authentication. Using a browser cookie as a VPN
password or adding a new VPN bearer credential is outside the approved design.

The proposed choice is therefore one VPN profile identity per admitted
application credential and range generation, bound permanently at issuance.
Repeated downloads under that same live credential reuse its current profile;
they do not mint a certificate on each download. A new browser session is a new
application credential under the approved session design and cannot adopt the
older profile's binding. This granularity is a Shifter integration decision,
not an OpenVPN recommendation. The profile remains an exportable private key:
its possession proves the bound VPN identity, not the identity of the person
using a copied key.

## Why a mutable credential reference is insufficient

Recording whichever credential last downloaded the shared profile would let a
later download replace the reference checked for an earlier key holder. A
revoked caller could still present the same certificate and be admitted under
the later caller's live authority. Binding every connection to the launch
credential also fails: launch permission is distinct from access permission,
and a valid administrator need not be the initiating owner.

## Recommended extension

Reuse native OpenVPN/X.509 authentication and the incumbent provisioner workflow:

1. At profile request, use the owning access service's exact decision:
   `range.access` for ordinary range access, or the CTF owner's
   `event.participate` decision plus live participant assignment to that range.
   Preserve the temporary credential's event ceiling. Capture that admitted
   action/target, credential UUID, original ceiling, customer scope and range
   generation in the existing operation transaction.
2. Issue distinct participant certificate/profile material for that admitted
   credential and generation, using the existing X.509 library and CA. Give
   each profile an opaque unique common name; persist its issuer, serial and
   certificate fingerprint with the immutable binding. Keep CA access in the
   provisioner. Store private material through the existing provider secret
   lifecycle and return only a non-secret receipt to the owning service.
3. Bind the verified certificate's public identity to that immutable record.
   Extend the authenticated pool-controller request with the peer certificate
   identity from OpenVPN's verified TLS metadata. Do not infer a browser actor
   from a shared certificate or accept caller-supplied identity as proof.
4. Reauthorize that exact initiating credential at connect, native `REAUTH`
   and heartbeat; a known session is not sufficient authorization.
   Preserve current generation/target/port checks, one active session per range,
   the 15-second heartbeat and 60-second portal lease. Native controller kill and
   firewall removal remain the data-plane termination mechanisms. Reject
   missing, revoked, expired or unknown profile bindings before admission.
5. For permanent credential/profile revocation, retire the immutable binding,
   revoke the associated certificate serial, publish an updated CA-signed CRL
   through the existing pool lifecycle, and terminate its active sessions.
   Temporary policy denial also denies admission and terminates affected
   sessions, without automatically making certificate revocation irreversible.
   Specify CRL refresh/expiry and publication failure behavior. The 2.6 manual
   warns that a missing CRL can allow a connection, so the directive alone is
   insufficient: verify CRL availability/freshness and fail closed in the
   existing controller/health lifecycle. The pool never needs the CA key.
6. Make first issuance/reissuance an explicit asynchronous profile operation;
   adapt the existing download UI/API to show preparation before delivering the
   validated profile. Preserve idempotency, uncertainty handling and cleanup.

The 15-second heartbeat and 60-second database lease are Shifter settings,
not OpenVPN reference guarantees. The current controller separately defaults
to a 90-second portal-outage grace period. The database lease does not itself
close a native tunnel. Measure healthy-policy revocation and control-plane
outage through observed tunnel closure, including call timeouts and cleanup;
do not advertise a 60-second data-plane bound from the database field alone.

Targeted qualification must cover independently identified clients, repeated
downloads without binding replacement, one credential's revocation while
another remains live, CRL rejection on reconnect, native termination of an
already connected client, missing/stale CRL handling and portal outage. Use
synthetic credentials and the configured OpenVPN 2.6 runtime. Full suites
remain in CI/CD.

This extends certificate issuance and its operation/API contracts beyond merely
adding a policy check to the existing heartbeat. It adds neither a new issuer
service nor a bearer/capability protocol, and it requires no new hosting or
production deployment. The user approved this issuance extension as part of
#2319's C4 source integration on 2026-10-14.

The VPN source integration remains an open obligation until implemented and verified.
The existing generation certificate must not be relabeled as a per-credential
proof, and #2319 must not be reported ready with this gap unresolved.
