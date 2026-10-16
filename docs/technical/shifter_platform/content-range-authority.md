# Content, range and continuing access authority

This documents the S6 source integration for #2319. Production authority is
activated by S8/#2321. Existing human entry points retain their incumbent
authority for legacy resources until that coordinated cutover; principal-owned
terminal resources always require a live credential. The `CredentialContext` entry
points authorize independent human and service principals through the shared
action catalog and evaluator.

## Resource and credential identity

Business ownership and historical attribution are distinct from the principal
performing an operation. Owning services resolve account, organization and
workspace ancestry from persisted facts. CMS and Engine range projections must
agree on the public CMS range UUID, runtime identity, request, ownership and
actual event parent. An account-level range has no artificial workspace.
An early failed launch with no native runtime remains visible to authorized
history/detail readers. CMS validates request custody and event ancestry and
asks Engine to establish runtime absence by request and public identity.
Only reads admit this unrealized intent; lifecycle and remote effects still
require a consistent runtime binding.

A credential UUID is a non-secret reference to an already authenticated
credential. It is never accepted as authentication by an HTTP endpoint. Durable
work resolves that reference through the credential's owner and intersects the
current ceiling with the originally admitted ceiling. A subsequent grant cannot
widen recorded intent. Logout, expiry, credential revocation, principal
revocation and changed resource scope prevent further user-authorized effects.

The management-owned database session backend extends Django's native session
model with a credential UUID and pinned principal UUID. Session cycling creates
a different reference. Jobs store neither a session key nor a cookie. Periodic
assurance checks save provider metadata without extending framework expiry or
resurrecting a deleted session. Database triggers prevent changing an existing
session key, credential UUID or authenticated principal through bulk writes.
Sockets retain the original reference and ceiling; a newly rotated session
cannot replace the credential that admitted an established connection.

## Durable operations

The CMS registration service accepts the authenticated credential and a resolved
parent scope. It retains the existing pack validator and version precondition,
records principal ownership separately from historical user attribution, and
keeps revisions in their original scope. Operator publication and unpublication
require `installation.publish_content` and an installation-owned source. Private
registration never publishes to another customer or the historical catalog.

Registered preparation resolves the source's actual scope before private
staging and repeats authority and immutable-source checks afterward. Admitted
images retain that scope and owner. Realizability and launch input selection use
the same Engine-owned filter; another customer's image cannot satisfy supply.
Image source names and versions are unique within their scope. Database guards
prevent changing that custody, including through bulk updates.

The incumbent launch intent, preparation and plugin records hold scalar
initiating principal, credential reference, original action ceiling and exact
scope. Source records protect immutable authority and executable/input identity
through database constraints, including updates that bypass model `save()`.
Policy checks occur before SQL critical sections; generation and persisted-fact
fences are checked again inside them. The credential owner supplies a local
lifecycle fence for these transactions; it checks logout, expiry, binding and
current limits without contacting an identity provider. This fence cannot admit
work: full credential and policy validation still precedes the transaction.
Personal-token fences likewise reconstruct only SQL-owned token lifecycle and
limits; the live token-use grant check runs during full admission before locks.
Principal launch intents separately pin
the installed provisioner namespace, service account, image, command and
deterministic Job identity. Dispatch refuses profile drift, and native interrupt
checks the original workload identity. Database guards protect that attribution;
an application principal UUID never substitutes for a worker identity.

Principal launches use the existing RAES resolver, digest verification, compiler
and CMS dispatch port. The actual account/organization/workspace parent supplies
launch authority; private content and image inventory retain their own read
decisions. Engine checks its parent independently. Prepared in-process admission
keeps provider checks outside the creation transaction; CMS/Engine linkage,
native operation input, outbox and strict launch audit commit together.
Account-level launches create no workspace or contact user. New principal
generations retain a lifetime UUID on both VM backends, independently of the
operation UUID used for dispatch/retry fencing.

Device creation extends the incumbent NGFW hydrator, interpreter and operation
outbox. CMS resolves same-scope, unexpired encrypted registration credentials
before hydration. Both owners retain the initiating principal and historical
human attribution separately. Native request custody is scalar and protected
by database guards. A principal may have an active device in each explicit
customer scope; the reservation remains occupied until accepted native
destruction is mirrored. Registration secrets in versioned Engine specs use
the existing field-encryption key. A forward migration encrypts retained native
NGFW specs through the same primitive.

The provisioner's raw SQL reader decodes these versioned encrypted registration
fields before AWS Terraform variables or GDC bootstrap files are prepared.
Missing keys or failed authentication of ciphertext abort that read. Human
devices retain deployed names; service-owned devices use the native device UUID
for provider names, namespaces and SSH-secret identity without a fabricated user.

CMS device specifications encrypt these same registration fields recursively,
including nested objects and lists. The forward migration covers retained and
retired device records. Failed devices remain reachable through the authorized
destruction path; their active slot is released only after accepted destruction.

Device lifecycle, SSH bootstrap and continuing access use registered parent
actions and original credential references. An accepted native teardown can
authorize explicit generation-fenced system cleanup after revocation only after
the existing interrupt worker suppresses dispatch and verifies provider task
absence. Launch-delivery success is not task-completion evidence. Provisioning
cannot authorize this cleanup; uncertain task absence remains on the incumbent
bounded interrupt/retry path. Device status callbacks must match the accepted native outbox payload, current
operation, resource identities, scope and owner. Rewritten or unaccepted
callbacks cannot change a principal-owned device or release its reservation.

Pause, resume, destroy and cancel stage native admission before the CMS/Engine transaction.
That in-process service result never becomes a client or worker credential.
The transaction fences the inspected resource, records the original proof in
the existing outbox, and commits both status projections and strict audit
records together. Anonymous incumbent calls cannot change principal-owned
ranges. Native scenario ranges retain their existing pause/resume capability
restrictions.

Destroy selects the existing native teardown command from the persisted plan.
While a soft-deleted range is still destroying, its validated scope remains
available to the original queued teardown; normal access continues to reject
the deleted projection. Dispatch rechecks the initiating credential, and a
destroy retry cannot mint another operation or resurrect a destroyed range.

Cancellation retains the existing generation-bound provision interrupt worker.
Direct destruction during RAES provisioning enters that same interrupt path;
it cannot queue teardown while the provision worker may still be active.
Its separate immutable admission record preserves the cancelling principal's
non-secret credential reference and original ceiling without overwriting the
provisioning principal. The accepted interrupt, both status projections and
strict audit commit together. After verified provider task absence, the worker
uses explicit cleanup authority derived from the original provisioning
generation. That limited cleanup can finish after either caller is revoked.
The incumbent interrupt supports native scenario provision generations;
unsupported generations are rejected before changing either projection.

Principal scope moves authorize the persisted source and requested destination
parents independently in CMS and Engine before acquiring range locks. Both
services fence current ownership, scope and lifecycle. The destination must be
live, and the business owner must retain explicit account membership there.
CMS request intent and both range projections move in one transaction with
strict audit. Native ownership reassignment similarly updates all four owner
projections and preserves historical human attribution. Immutable event
aggregates and ranges with unretired VPN custody cannot move. Anonymous legacy
Engine commands cannot rebind principal-owned ranges.

Accepted scope and owner changes retain immutable receipts with the admitted
principal, original credential ceiling, previous binding digest and new
generation-bound custody. The existing teardown outbox can use these receipts
as explicit cleanup origins; they introduce no delivery worker or job state.
Server lease expiry reads the current persisted deadline, rejects caller-only
expiry overrides, and uses this bounded cleanup authority after revocation.
Provisioning expiry first records the existing interrupt and waits for verified
task absence. An expired range can replace a revoked caller's queued teardown
with a system cleanup generation while preserving the original proof.

Scenario metadata changes resolve the registered source's scope, retain the
incumbent realizability gate, repeat live authority after assessment, and fence
source identity before committing the overlay with principal audit. A service
change records its principal without assigning a historical human updater.
Scenario list, detail and launchable-list service entry points use the same
scope-filtered registry as native catalog queries.

Range scope moves apply the current destination workspace quota and move the
native reservation with the projections. Returning to a previously released
workspace slot requires a new capacity decision; a replay cannot bypass a
changed hard cap. Quota decision and canonical audit records retain the
principal UUID. Quota policies currently exist only for actual workspaces;
unconfigured scopes retain the incumbent absence-of-policy behavior.
Transfers acquire the destination account membership mutex before quota
workspace mutexes, matching launch admission and avoiding a lock-order cycle.
Service-owned ranges use the live tenant/default lease policy. A human business
owner continues to receive restrictive policies from their actual Django groups.

Principal runtime status changes enter through the native operation result
applier. Legacy status callbacks cannot match a principal range by a missing
human user ID. CMS status notifications must match an existing native outbox
payload and the current runtime generation, scope, owner and accepted status.
They mirror accepted evidence and cannot authorize a runtime mutation or
rehome a CMS projection. Rewritten and stale callbacks are discarded.

Authenticated preparation receipts remain available as evidence after the
initiator loses authority. Receipt collection cannot admit inventory or advance
work. Dispatch, retry and result application independently check the original
authority. Cleanup is explicit, limited to the original resource and generation,
and does not acquire a user grant from a missing actor.

Each source preparation attempt separately pins the native Kubernetes namespace,
dedicated role service account, deterministic Job identity and approved image.
These facts come from the installed preparation grant and incumbent worker
profile. Builder, verifier and cleanup identities remain separate from the
initiating application principal. Database guards protect that attempt identity
and prevent rewriting a recorded receipt while allowing lease and reconciliation
state to advance.

## Remote session termination

Scoped installer uploads use the incumbent HMAC-signed completion token and
provider upload URL. The CMS records the original credential reference, exact
ceiling, scope, quota reservation and distinct staging/install keys before URL
issuance. It stores neither returned token nor URL. Source upload URLs last at
most five minutes; revocation prevents new issuance and finalization but does
not recall an already delivered provider capability.

Finalization checks the original proof before provider I/O and again before
catalog admission. Header inspection and immutable copying bind to the observed
S3 ETag or GCS generation. Retries return the original installed asset. A
cancelled or deleted asset cannot be resurrected by replay. Database guards
protect upload custody, credential proof, storage location and admitted evidence.

Cancellation retains its quota reservation until expiry cleanup succeeds.
Cleanup uses the recorded provider, bucket and fixed upload namespace. It
preserves active installed assets, removes retired or failed material, and keeps
evidence on uncertainty. S8 must schedule `prune_agent_uploads`; this source
integration does not activate a production scheduler. Imported historical assets
need qualified storage-custody evidence before this cleanup path can retire them.
Cleanup acknowledgement takes the same budget lock as installer retirement and
rechecks the retirement snapshot, preserving a concurrent deletion request.

Remote service entry points preserve native RAES member keys as strings. They
accept the public `range_uuid` to select the exact owning range; an ambiguous
unqualified member key fails closed. Guacamole workers and renewals retain the
admitted range context, so another range with the same member key cannot change
an established session's resource binding.

The Channels boundary rechecks credentials at connect, input, protected output
and while idle. The configured interval is at most 15 seconds, with a five-second
validation timeout. The default composition calls the owning CMS service for
the terminal guest or range-status subscription on the actual route. Status
events must match that subscription's request. These
bounds apply to detecting denial; provider assurance may have its own cache
window, and native transport shutdown is a separate step. Tests use shorter
explicit intervals to exercise notification loss, denial and evaluator failure.

Browser terminal admission reconstructs the live Django session credential
from the Management session backend before principal resource authorization and
pins that reference for subsequent checks. The incumbent Django backend retains
legacy workspace admission and cannot authorize principal-owned resources.
Historical `Range.user` and `Request.user` values cannot authorize a
principal-owned guest. Legacy-only resources retain their incumbent workspace
admission. Ownership transfer closes the former owner's established terminal
and denies another connection even when the historical user fields remain.
S8 must select `management.session_backend` as Django's `SESSION_ENGINE` with
the coordinated principal HTTP/socket admission; deploying these source paths does not silently
replace the incumbent session store or migrate existing browser sessions.

Revocation wakes the consumer's disconnect path and cancels pending SSH
connection work. Native SSH connection and shell setup each have a 15-second
timeout. Close waits are bounded to five seconds, followed by transport abort.
Reader tasks and capacity reservations are released once.

Guacamole uses the pinned 1.6.0 server's provider-independent
[`DELETE /api/session`](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/session/SessionResource.java).
Its [session invalidation](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/GuacamoleSession.java)
closes the associated tunnels. Shifter sends the native token in the
`Guacamole-Token` header and refuses redirects. Bootstrap URLs and retained
native session tokens use the existing field-encryption key.

The native qualification test establishes actual SSH and SFTP connections through
the repository's pinned Guacamole and guacd images. Both guest connections close
within the tested 20-second bound after session invalidation. An HTTP success
response precedes completion of native shutdown; it is not an instantaneous
guest-disconnect timestamp. The gateway timer and bounded renewal queue also
have the concurrent-context qualification described below.
The fixture holds healthy SSH/SFTP sessions for 35 seconds with protocol and
WebSocket-tunnel keepalives before withdrawing authority. It also discards an
actual native DELETE acknowledgement and verifies repeated cleanup converges.

The approved gateway extension additionally uses Apache's supported
`UserContext` decorator. Principal bootstraps sign `access-<bootstrap UUID>` as
the JSON-authenticated identity. The gateway sends only this opaque ID to
`/api/v1/mission-control/guacamole/control/authority/`. Google's SDK supplies
the gateway's metadata-issued ID token; the portal verifies the configured
audience, service-account email and immutable subject, then checks the original
recorded application credential, ceiling, range generation and scope.

The local monotonic lease is 90 seconds and renewals run every 15 seconds.
Denial or expiry permanently invalidates that native session. Guacamole's
one-minute native eviction cadence remains part of the shutdown bound. Local
synthetic native SSH/SFTP qualification measured guest closure at 73.02 seconds
after denial and 120.03 seconds during an unavailable control service. Those
measurements establish these fixture outcomes; they do not establish a loaded
production guarantee. Native RDP qualification additionally verified a live
guest desktop and its socket table before and after renewal failure. That
fixture measured transport closure at 33.80 seconds after denial and 138.89
seconds during a control outage, within its 85/165-second test bounds.

The bounded registry admits at most 512 decorated contexts. All admitted contexts receive
a renewal queue slot, with at most four simultaneous control requests. The
512-context concurrency test covers admission, queue capacity and renewal;
the blocked-worker test verifies local expiry independently of control progress.
These component checks do not establish a 512-tunnel production shutdown bound.

The image builds the pinned 1.6.0 extension and installs it through Apache's
entrypoint extension hook after runtime-home generation. Helm and the GCP base
use a dedicated `guacamole` workload account, with no project roles or platform
secret access. The coordinated S8 activation must supply the common
`OPENFGA_ENABLED`, `GUACAMOLE_CONTROL_URL` (an administrator-controlled HTTPS
origin), `GUACAMOLE_CONTROL_AUDIENCE`, and the portal's exact
`GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_EMAIL` and
`GUACAMOLE_CONTROLLER_SERVICE_ACCOUNT_ID`. Missing workload configuration
refuses bound sessions. No separate Guacamole policy mode is introduced.
Activation also requires destination-specific HTTPS egress to the configured
control origin, alongside the existing metadata-server egress; do not broaden
gateway egress to arbitrary Internet destinations to make renewal work.

The existing bootstrap controller revalidates source sessions. A failed native
termination keeps the encrypted reference for another attempt. Pruning preserves
expired undelivered successes until native termination is confirmed. Such
bootstraps stop renewing even if the original credential remains valid; a
conditional database update protects a handoff that completed before expiry.
Delivered native sessions continue through their normal live-authority checks.
Pruning also preserves in-flight source bootstrap work and sessions awaiting
termination. Issuing or expiring a browser URL does not establish backend teardown.
After an ambiguous DELETE or a failed local acknowledgement, the pinned native
session endpoint's specific missing-token response confirms absence and allows
the retained reference to be cleared. Generic proxy denials, malformed replies
and timeouts remain uncertain. This follows the upstream
[session lookup](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/session/SessionRESTService.java)
and [authentication service](https://github.com/apache/guacamole-client/blob/1.6.0/guacamole/src/main/java/org/apache/guacamole/rest/auth/AuthenticationService.java)
contracts; URL expiry is never used as absence evidence.

Run only the native qualification target when this transport changes:

```bash
cd shifter/shifter_platform
TESTING=1 TEST_DB_BACKEND=sqlite GUACAMOLE_NATIVE_INTEGRATION=1 \
  GUACAMOLE_EXTENSION_IMAGE=shifter-guacamole-2319-local \
  uv run --no-sync pytest -n 0 tests/integration/test_guacamole_native_termination.py \
  tests/integration/test_guacamole_native_rdp.py -q
```

The fixture uses an isolated local Docker network, localhost HTTP and a
disposable synthetic SSH/SFTP directory. It removes its containers and network.
Full repository suites remain the responsibility of CI/CD.

## OpenVPN credential identity and revocation

The existing provisioner issues a distinct certificate/key and opaque
`access-<profile UUID>` common name for each admitted application credential and
range/runtime generation. Repeated downloads retain that binding. Reissuance
creates a new identity and preserves the retired row. The portal receives only
public certificate metadata and provider references; the CA key stays with the
existing issuer. This uses OpenVPN's [client PKI guidance](https://openvpn.net/community-docs/setting-up-your-own-certificate-authority--ca--and-generating-certificates-and-keys-for-an-openvpn-server-and-multiple-clients.html).

The pool forwards `tls_serial_0` and `tls_digest_sha256_0` from verified TLS
metadata to the existing control API. Admission and renewal resolve the original
credential, ceiling, range generation and current participation. Client-provided
identity hints do not replace that proof. Preparation returns `202
vpn_preparing`; download responses remain private and non-cacheable, with shared
rate limiting and principal audit attribution.

Permanent revocation retires the binding and publishes a standard CA-signed
CRL. The existing workload interrupt confirms issuer termination before cleanup
reads a potentially uncertain stored certificate. The revocation receipt retains
that certificate's public identity. Private material is removed only after the
receipt is accepted and the applicable revocation is published, so retries do
not lose their evidence. Issuance and cleanup retain their original immutable
operation inputs and authority. A temporary lookup or evaluator outage denies
access without itself irreversibly revoking a certificate.

The immutable issuance input pins the existing Secret Manager project,
deployment namespace and issuer reference. Cleanup and refresh copy those facts
from the issuance input, so later configuration changes do not redirect their
provider operations. Exhausted issuance dispatch and a missing receipt fifteen
minutes after dispatch retire the pending identity and enter the existing
interrupt/recovery path. Refresh, revocation and purge also enter bounded native
interruption when dispatch succeeds without a receipt for fifteen minutes, when
delivery is exhausted, or when an accepted failure requires retry. Replacement
waits for provider task absence and retains the original issuance/storage proof.
Revocation during an active refresh interrupts that worker before replacing it.
Native operation failures carry a fixed diagnostic;
private provider or key-parsing exception text is excluded.

CRLs do not immediately terminate established tunnels, as the
[OpenVPN revocation guide](https://openvpn.net/community-docs/revoking-certificates.html)
explains. The controller removes forwarding and invokes native `client-kill`.
The nftables established-flow rule requires the original connection tuple to
remain authorized, including return traffic through NAT. Failed rule removal,
uncertain management termination and asynchronous controller failure stop the
pool. A management reply timeout closes its socket so a late reply cannot
acknowledge another command. OpenSSL verifies the signed CRL before atomic
installation; missing, modified, expired or rolled-back files fail closed.

Native qualification uses OpenVPN 2.6.20, OpenSSL 3.5.9 and nftables 1.1.3 from
the locally built pool image. It verifies distinct client identities, verified
TLS serial/digest delivery, native disconnect, CRL rejection on reconnect and
an independent unrevoked client. The control-outage fixture exercises the
default 90-second grace and asserts actual disconnect within 90–100 seconds
while checking every 250 ms. This fixture bound is distinct from production's
15-second heartbeat and network/management call delays. The database lease
alone is not a native tunnel-closure bound.

The isolated forwarding fixture additionally verifies that revocation blocks an
already-established TCP flow. Both fixtures use synthetic material, no external
network, and no published ports. Run these exact targets with a locally built
image; the outage case deliberately takes roughly two minutes:

```bash
SHIFTER_VPN_NATIVE_IMAGE=shifter-openvpn-2319-local \
  PYTHONPATH=shifter/engine/openvpn shifter/shifter_platform/.venv/bin/python \
  -m pytest shifter/engine/openvpn/tests/test_native_forwarding.py -q
cd shifter/engine/provisioner
SHIFTER_VPN_NATIVE_IMAGE=shifter-openvpn-2319-local SHIFTER_VPN_QUALIFY_OUTAGE=1 \
  PYTHONPATH=.:../openvpn:../..:../../shifter_platform \
  ../../shifter_platform/.venv/bin/python -m pytest tests/test_vpn_native_pki.py -q
```

S8 must propagate the existing coordinated authority configuration to the pool
and seed a valid signed CRL before starting it in that mode. Pool startup
requires that CRL. Production activation and the remaining transport/load
qualification remain separate from these completed native fixture checks.
