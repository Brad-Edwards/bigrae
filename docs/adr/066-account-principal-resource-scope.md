# ADR-066: Stable accounts, independent principals, and explicit resource scope

## Status

Accepted for the identity and authorization redesign agreed on #2308. Issue
#2314 is the first schema and mapping slice. Runtime cutover remains S8.

## Context

The current tenancy model gives every Django user a synthetic personal
organization and workspace. It also treats one deployment as one customer
boundary. Those assumptions cannot represent an individual customer without
invented subdivisions, a person who belongs to several customer accounts, an
independent service identity, or shared B2C and B2B hosting.

The useful incumbent boundaries remain sound: `workspaces` owns customer scope
and membership policy, `management` owns application identity, `shared` owns
dependency-neutral contracts and cross-cutting primitives, and other domains
carry validated scalar scope references rather than cross-layer foreign keys.

## Decision

### Customer hierarchy

`workspaces` owns one stable, publicly opaque `Account` root with a closed type.
An individual account has no organization or workspace. A team or enterprise
account has one default organization and may have more; every organization has
one default workspace and may have more. An organization belongs to exactly one
account and a workspace belongs to exactly one organization.

Creating the type-required default structure is an idempotent domain operation.
It does not infer or create a membership, owner, administrator, policy, or cloud
grant from the caller, creator, Django user, provider claim, or account type.
Any grant is a separate explicit mutation.

Existing organization and workspace primary keys and UUIDs remain stable when
the row has a valid place in the new hierarchy. The words account,
organization, and workspace describe distinct facts and are not aliases.

### Principals and provider bindings

`management` owns stable `Principal` records with a closed human or service
kind. A human principal retains the existing Django user as its authentication
and lifecycle record. A service principal has no fabricated Django human and is
not the principal of its creator or responsible contact. Changing or removing
creator/contact responsibility does not change the service principal identity,
memberships, policies, or owned resources.

Provider identity is an immutable binding from an exact, opaque
`(issuer, subject)` pair to one principal. The tuple is unique. Email, username,
provider group, sign-in method, or subject without issuer is not an identity
key and cannot merge, repair, or rebind a principal. A collision fails closed.
The existing `shared.verified_identity.VerifiedIdentity` gate remains the only
provider-evidence shape admitted by the Cognito/OIDC and Identity Platform
adapters.

Email-free temporary CTF users remain supported as human principals without a
provider binding. Their durable `UserProfile.is_ctf_account` origin marker,
password-change gate, event-native authority, and provider-login exclusions
remain authoritative.

### Membership and authority

Customer membership targets a principal, not a Django user, email, provider
identity, API token, or creator relationship. Account, organization, and
workspace memberships are explicit facts at their own scope. A membership or
role at one scope grants nothing at another scope unless the owning policy
explicitly composes it.

The existing `workspaces.services` boundary stays the customer-scope owner and
the only resolver of SQL-owned resource ancestry. Cross-domain consumers
receive immutable scalar projections or authorization results and never import
workspaces models. OpenFGA becomes authoritative for application permission
relationships at the S8 cutover; SQL continues to own identities, resource
state and ancestry, and display metadata. SQL rows must not mirror or evaluate
OpenFGA relationships. Provider groups, Django groups, token scopes, CTF roles,
cloud IAM, and cached UI capabilities do not become alternate application
permission stores or implicit grants.

### Shared principal and scope contracts

`shared` owns one dependency-neutral principal reference and one closed
`ResourceScope` contract. The scope has an explicit discriminator:

The principal reference carries only the stable opaque principal UUID and its
closed human/service kind. It carries no user, provider, credential, account,
membership, role, or policy data.

- installation scope has no account, organization, or workspace identity;
- account scope requires an account identity, may include its organization,
  and may include a workspace only with its organization.

Missing customer identifiers never means installation scope. The shared
contract rejects malformed shapes; `workspaces.services` resolves identifiers
and rejects an organization from another account, a workspace from another
organization, archived or otherwise inadmissible scope, and any subdivision on
an individual account. Owning domains persist the validated scalar scope facts
they require. They do not add a cross-layer foreign key, JSON scope bag, copied
hierarchy, or local scope validator.

CTF event creation authorizes against one existing account, organization, or
workspace parent before the event exists. Personal events use their individual
account directly. Business events use validated organization or workspace
ancestry. The OpenFGA event object has exactly one parent relation matching the
SQL-owned scope; extra or stale parent edges deny, including for event-derived
ranges. The shared action catalog registers parent-scoped creation and the
separate CTF management distinctions. Creator attribution, principal ownership,
event administration, and competition participation remain separate facts.
S5 prepares and tests these contracts; S8 activates them together with the rest
of the application authority cutover.

### Mapping and cutover

Current users map to human principals by durable user ID. Existing provider
tuples map to provider bindings only from complete, valid evidence. Existing
membership and ownership rows map through that user-to-principal relation, not
through email, name, group, or ordering. Existing organization and workspace
rows keep their durable IDs when their ancestry is valid.

Legacy personal-workspace data maps to an individual's account-level scope only
when the current ownership projections and synthetic personal structure make
that mapping unambiguous. Shared organizations cannot be guessed to be team or
enterprise accounts. Subject-only provider rows cannot be assigned an issuer by
guessing. Extra members, conflicting owners, cross-layer projection drift, or
other contradictory evidence stop mapping with bounded internal row
identifiers. Mapping never deletes, silently reassigns, or fabricates a grant to
make inconsistent data fit.

Source changes may establish and test the new schema before S8, but they do not
activate a mixed authority model. There is no dual-write compatibility window,
read fallback, customer-specific deployment requirement, feature-flagged
policy fork, rollback to the personal-workspace model, or partial production
cutover. S8 is the activation point after all mappings and constraints pass.

### Hosting and policy boundaries

One deployment may host unrelated B2C and B2B accounts. Account scope is an
application authorization and data-partition fact; it does not by itself prove
separate PostgreSQL, encryption-key, cloud-project, network, secret, workload
identity, or incident boundaries. Those guarantees require their own effective
controls and evidence.

Application, cloud, organization, workspace, event, participant, operator, and
external-client authority remain distinct and compose only through their owning
service boundaries. Deliberately granting one principal full application and
cloud authority is supported as two explicit grants. No role or account type
implies the other grant. Dependency loss never creates permission or a
successful effect.

The redesign uses provider-native GCP identity, IAM, and renewal; OpenFGA for
application policy; established Django mechanisms; and django-rest-knox in the
slices that own those integrations. This ADR does not authorize a custom
authentication protocol, policy engine, token format, secret broker, JIT/Vault
dependency, or organizational approval workflow.

### Administration at cutover

At S8, every application administration operation, including its direct service
call, uses the registered action, resolved target/scope, live principal, and
credential ceiling through the shared authorization contract. Human and service
application administrators can perform the same valid application operations
when their credentials permit them. Django staff/superuser flags, model
permissions, customer membership, and browser capabilities remain distinct
facts; none substitutes for an application policy decision. A browser-only
credential ceremony still requires its own session and CSRF admission.

This supersedes ADR-045-R3's staff-session-only audit-read gate at S8: the
deployment-global ledger is read through `installation.read_audit`, with the
same policy check for its list and detail views and an authorized credential.
The current ledger has no trustworthy customer scope per row, so a customer
role cannot filter or read it by inferred entity, actor, selected workspace,
or request parameter. Tenant-visible audit would require explicit scope at
emission, integrity-protected persistence, and a separately registered scoped
read action. Historical rows without such evidence remain installation-only.

The same S8 policy boundary supersedes the Django-superuser-only application
override in ADR-046-R13, quota authoring in ADR-046-R10, and organization
overrides in ADR-048-R2/R3 for operations delivered by the new administration
surface. Source and destination scopes must each be resolved and authorized for a transfer;
existing owner, membership, lifecycle, cloud, and consistency constraints still
apply. Retained Django-admin or legacy browser writes enter the same authorized
service command or are disabled once a supported canonical UI/API path exists.
These changes do not grant a service principal a Django admin session, make
cloud IAM an application grant, or alter historical audit evidence. Before S8,
the established gates remain the sole production authority.

The OpenFGA application-policy boundary, supported SDK operations, durable
write semantics, deployment contract, and cutover constraints are clarified by
`docs/architecture/openfga-authorization-preflight-2315.md`. That clarification
does not move the S8 activation point or authorize mixed runtime authority.
Delegation proofs enumerate a bounded, complete descendant set from the
SQL-owning domain service boundaries before asking OpenFGA to evaluate it;
provider graph discovery is never authoritative ancestry. A blocking Quality
job runs the released OpenFGA/PostgreSQL harness whenever platform changes are
selected, so missing harness configuration or skipped conformance cannot pass.
The general PostgreSQL lane deselects the `openfga` marker because that service
posture is owned by the equally mandatory released-server job, not by a bare
PostgreSQL service. Both jobs retain the PostgreSQL fail-on-skip guard; the
OpenFGA harness also requires its server configuration explicitly. CI contract
tests keep the lane selection and mandatory dedicated job coupled.
Each descendant retains its SQL scope during delegation checks. Durable
operations bind their provider store and model; explicit HTTP recovery checks
the caller's current authority for the recorded effect before replay. Helm
migration service-account and egress prerequisites remain present until the
dependent job finishes, including first enablement on an existing deployment.

### Content, operations and continuing access at cutover

S6 extends the existing CMS/Engine/Mission Control service, durable-operation
and channel contracts; its integration boundaries are recorded in
[the content and range authority preflight](../architecture/content-range-authority-preflight-2319.md).
Customer content, credentials, installations and preparation use explicit
resolved account scope. Operator-shared catalog access never implies access to
customer-private assets or executable installation authority. Account-level
ranges must be represented in the canonical action/model/ancestry and descendant
contracts without inventing a workspace.

Principal RAES creation carries exact customer scope and public identity through
the incumbent dispatch port and `RangeBindings`. Engine evaluates parent
authority before the creation transaction. CMS linkage, native input, queued
effect and strict launch audit commit together with fresh SQL/credential fences.
Both VM backends retain a principal resource-generation UUID for continuing
access and bounded cleanup.
Failed CMS launch intents remain readable before Engine runtime creation.
History/detail reads validate request custody, actual event ancestry, current
credential and leaf policy, and establish runtime absence through Engine's
service boundary. Runtime actions still require consistent CMS/Engine custody.

NGFW creation and lifecycle extend the existing interpreter and outbox with
scalar request custody and original non-secret credential references. CMS and
Engine request custody is immutable through bulk-write guards. Scoped device
slots are released by accepted native destruction evidence; callbacks cannot
substitute resource identities or scope. Registration secrets reuse the existing
shared encrypted JSON field and key throughout nested CMS and native spec
envelopes. Forward migration includes retained and retired CMS records.
The raw provisioner SQL boundary decrypts registration fields before native
bootstrap preparation. Service device names and secret custody derive from the
native device UUID; a nullable historical human ID never becomes a fake user.
Device SSH uses the same opaque bootstrap, workload-authenticated renewal and
native termination boundary as range SSH. Explicit system cleanup can replay an
accepted teardown after revocation only after suppressing further dispatch and
verifying native task absence through the existing interrupt boundary. Delivery
success is not workload completion. Provisioning cannot mint this proof.

Prepared image admission retains the original application owner and resolved
customer scope. The image registry's natural key includes that scope, and
database guards prevent a scoped image from being rehomed through bulk updates.
Preparation, realizability and range input materialization select the same
scoped inventory through the Engine service boundary. Historical global
catalog reads exclude principal-owned private sources and images. Shared
catalog publication requires an explicit installation-owned source and the
registered publication action; customer ownership never implies publication.
Registration and revision retain the existing pack validators, optimistic
version preconditions and strict transactional audit path, with current
credential checks repeated after private staging or validation.

Principal range scope and ownership moves preserve the established CMS-owned
orchestration boundary. Engine independently authorizes its native projection
and consumes exact scalar ancestry and business-owner membership facts through
the narrow read-only `workspaces.services.resolve_range_scope_binding` facade.
That facade performs no tenancy mutation and grants no application action.
The ADR-001 per-symbol import guard permits this fact reader; tenancy CRUD,
role changes and application transfer orchestration remain excluded. Admission
occurs before SQL critical sections, and commit fences scope, owner, lifecycle
and destination membership before changing projections and strict audit together.
Destination account membership mutexes precede quota workspace mutexes in both
launch and transfer. Personal-token commit fences reconstruct local lifecycle
and ceilings without provider I/O; full live grant admission precedes locks.

Accepted range transfers retain immutable non-secret admission receipts and
generation-bound current custody. These receipts extend the existing cleanup
origin lookup; they are not a new delivery store or job framework. Expiry uses
the persisted server deadline and explicit bounded system authority, including
after caller revocation or an authorized owner/scope change. A provisioning
generation must be interrupted and confirmed absent before teardown. No caller
can mint cleanup by supplying a missing actor, deadline override or another
resource's receipt.

Scoped installer upload extends the existing signed completion-token and
conditional-copy contract. Native CMS records bind the original credential,
ceiling, customer scope, reservation and immutable provider/storage identities
before capability issuance. No returned bearer token or URL is persisted.
Header reads and installation copy use the observed provider object version.
Cancellation preserves reservations until expiry cleanup succeeds; cleanup is
limited to the recorded namespace and preserves active installed assets.
Database guards protect the admission and accepted evidence. Historical user
attribution cannot expose principal-scoped content through an unscoped read.

Authenticated session references pin the Django session key, non-secret
credential UUID and principal through database guards, including bulk updates.
Credential display-name uniqueness for principal-owned records includes the
authoritative customer scope; nullable historical user attribution cannot bypass
that constraint. Socket resource checks use owning CMS services and preserve the
original credential reference and ceiling across every revalidation.

Durable operations preserve the initiating principal, separate executing
workload, non-secret credential reference and original exact ceiling, and the
admitted action/target/scope and generation. Dispatch and retry reauthorize
current authority without widening the recorded intent. Revocation cannot
silently substitute an owner or rehome work. Required cleanup has explicit,
bounded system authority and retains operation evidence; a missing actor never
grants that authority. Existing idempotency, outbox/inbox, cancellation and
cleanup-verification state machines remain authoritative.

Continuing remote access requires both credential and resource authorization,
revocation notification/revalidation and actual data-plane termination within a
documented, tested finite bound per transport, including idle connections and
dependency failure. Connect-time checks, browser closure and token expiry alone
do not establish this guarantee. These decisions preserve the isolated plugin
worker/public SDK boundary and the coordinated S8 activation point.
Browser terminal admission resolves a live session credential before accessing
principal-owned resources. Historical human attribution cannot authorize those
resources, including after an owner transfer; legacy-only resources retain
their incumbent workspace admission until coordinated activation.

Guacamole retains its native JSON authentication and tunnel lifecycle. The
approved #2319 extension uses Apache's UserContext decoration API to attach a
gateway-local monotonic authority lease to the signed bootstrap identity.
Privileged views and redecoration retain the same original lease; denial and
expiry are irreversible for that native session. A bounded renewal pool calls
the owning portal service with Google's metadata-issued workload identity.
The portal verifies the configured audience, email and immutable subject before
reauthorizing the original recorded application proof. Workload identity grants
no application permissions. Renewal failure cannot move the local deadline;
Apache's native context eviction closes active tunnels even during a portal
outage. Qualification includes its one-minute eviction cadence and actual guest
transport closure. The Java component has native compilation/test ownership and
joins the established Sonar analysis and coverage gate; legacy container-image
quality gaps do not exempt its runtime source.
Native cleanup accepts the pinned session endpoint's specific already-absent
response after ambiguous successful invalidation. Generic HTTP failure retains
the encrypted session reference for retry. Native qualification includes lost
acknowledgements, repeated DELETE, protocol keepalives and a healthy-session hold.
An expired bootstrap whose URL was never delivered is retired even while its
original credential remains valid. Cleanup retains its encrypted native token
until termination is confirmed and checks delivery atomically before claiming
expiry cleanup. Delivered sessions continue under the live authority lifecycle.

OpenVPN follows its existing Community 2.6 PKI and management boundaries:
each admitted credential/range generation receives a distinct certificate/key
and opaque common name. The issuer retains the CA key. Signed CRLs reject
revoked certificates on reconnect; the existing management controller removes
forwarding allowances and invokes `client-kill` for established tunnels.
Established forwarding also requires the connection's original tuple to remain
in the allowed set. Missing or stale CRLs deny admission, and uncertain rule
removal or native termination stops the pool.

The existing launch outbox, immutable operation input and result inbox own
issuance, revocation, CRL refresh and private-material removal. Cleanup first
uses the existing workload interrupt to confirm issuer absence, preserving
identity verification and its bounded failure dispositions. An uncertain
maintenance operation also requires native absence before replacement: exhausted
delivery, accepted failure, and missing bounded completion evidence enter the
same interrupt worker. Original issuance and storage custody remain unchanged.
An uncertain
certificate is recovered from its existing provider secret; that secret remains
available across retries until signed revocation evidence is accepted and
published. Session/profile custody and monotonic CRL publication have database
guards that also cover bulk updates. The implementation and qualification
details are in [the continuing-access documentation](../technical/shifter_platform/content-range-authority.md).

Engine may consume `workspaces.services.resource_scope_from_ids` solely to
validate its persisted scalar range ancestry before a native runtime decision.
ADR-001's per-symbol allowlist names this read-only seam. Engine gains no
workspace models, control-plane mutations or human-membership fallback through
it; CMS remains the control-plane owner.

## Supersession

S3's credential integration is documented in
[the credential boundary and client contract](../technical/shifter_platform/access-credentials.md)
and [its preflight](../architecture/unified-credential-auth-preflight-2316.md).
It uses Knox and Google/Firebase public SDKs, immutable SQL credential metadata,
live personal-token eligibility and shared HTTP/Channels session validation.
Personal credentials retain an immutable exact target in their shared action
ceiling; policy requests and mutation journals preserve that target dimension.
Principal-based CTF admission uses an explicit event-scoped API and the existing
participant-safe projection and registration/status eligibility predicate.
Credential-authorized mutations propagate canonical principal UUID attribution
through the shared audit contract and durable authorization journal. Audit v2
hashes that indexed identity while retaining the frozen v1 historical profile;
the migration advances the append profile without rewriting committed evidence.
`config` may consume `workspaces.services.hierarchy_target_scope` solely for
credential-issuance ancestry; it gains no workspace model or mutation access.
The standalone `shifter_client` package is owned by the platform quality unit,
with native-renewal tests in the platform test suite. These source and enforcement
changes preserve the coordinated S8 activation boundary.

This decision supersedes:

- ADR-045-R3's staff-session-only audit read when the S8 policy cutover occurs.
  Shared audit ownership, append-only integrity, and denied-read evidence remain.
- ADR-046's per-user personal organization/workspace compatibility default and
  the parts of its membership contract that use a Django user as the durable
  member identity. Its workspaces-domain ownership, scalar cross-layer
  references, service authorization, locking, opaque denial, and distinct
  authority rules remain incumbent.
- ADR-048's bootstrap administrator inference for personal organizations. Its
  explicit organization authority, service ownership, UUID-only public
  identity, transaction, and strict-audit rules remain incumbent for real
  organizations.
- ADR-054's one-customer-per-deployment decision and its personal-workspace
  migration assumption. Its least-privilege, distinct-authority,
  deployment-local effect ownership, fail-closed outage, and real-boundary
  evidence requirements are carried forward here.

PLAT-2011, PLAT-232, and PLAT-233 must be reconciled through Ground Control when
implementation begins. Historical migration files remain immutable evidence;
new forward migrations replace the active invariant.

## Consequences

The model can represent an individual without fake subdivisions, one person in
several accounts, and a service whose identity outlives its creator. It also
requires explicit account scope on customer data and explicit ancestry checks
at every owning service boundary. Current personal-workspace shortcuts cannot
be retained as hidden defaults.

Shared-hosting support increases the importance of negative authorization,
mapping, and provider-isolation evidence. Schema presence alone does not claim
that S8 cutover or live multi-customer isolation has occurred.
