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

The [S8 integration preflight](../architecture/integrated-identity-cutover-preflight-2321.md)
and [cutover proposal](../design/identity-cutover-2321-proposal.md) define the
single-authority activation, retained-data dispositions, first-grant bootstrap,
credential/session invalidation, complete effect-surface inventory and the
durable journaled boundary between SQL and OpenFGA. A fresh installation starts
only in the post-cutover state. A legacy-data upgrade uses the narrow existing
migration and deployment seams and remains stopped until terminal verification;
it does not restore legacy authority on failure.

Cutover readiness must not prevent deployment-owned migration/bootstrap commands
from running before the new schema or policy is available. Preserve the existing
authorization journal, domain outbox/inbox, strict audit and bounded cleanup
contracts rather than adding parallel workflow machinery. The 2026-10-25 owner
approval authorizes C6 source work but does not waive live acceptance evidence.

The `config` composition root coordinates this cross-domain cutover only through
explicit public service symbols. Management owns principal/provider/session and
token changes; workspaces owns account mapping, hierarchy relationships and the
durable cutover journal; CMS, CTF and Engine inventory and convert only their own
retained resources. Direct domain-model imports and pass-through services remain
forbidden. Engine's four cutover symbols (apply, disposition, pending-work and
contract-gap inventory) are the narrow exception that lets the composition root
reach Engine-owned rows without making CMS a false owner.

S8 verifies default existence as well as uniqueness throughout the retained
business hierarchy. It reconciles legacy and prepared membership facts against
their database identity constraints, preserving valid durable rows without
creating implicit grants. Final scope constraints are conditional on explicit
scope kind; account-only resources must pass the existing operation parsers,
SQL-owned descendant inventories and owning-domain projections without invented
workspaces or users. The preflight records these integration boundaries.

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

S7 (#2320) preserves deliberately assigned project-admin operator service
accounts and permits the same service full application authority through a
separate explicit grant. Protect credential use, impersonation and execution
boundaries; do not silently reduce chosen powers or broaden ordinary workload
defaults. Supported single-project deployment supersedes ADR-008-R7's mandatory
distinct dynamic-secret project and migration-only equality rule for S7. The
explicit project seam and named/prefix-conditioned access controls remain;
parent-scoped create-only access is not a prefix or separate-project boundary.
[The GCP integration preflight](../architecture/gcp-native-identity-authority-preflight-2320.md)
records the native renewal, SQL-duty, configuration/admission and OpenFGA trust
limits. Preshared client authentication and runtime method allowlisting do not
claim server-enforced per-client model/store privileges. Enforcement must be
reconciled with these documented choices without blanket exemptions. This
clarification does not approve custom source or move S8 activation.

### AWS conformance boundary (#2313)

The [AWS conformance preflight](../architecture/aws-identity-conformance-preflight-2313.md)
maps the merged S8 contracts to the existing AWS authentication, installation,
IAM and runtime boundaries. AWS retains this Account/Principal model and the
same OpenFGA application authorization semantics. Native cloud authority and
application admission remain separate explicit grants. #2308 closed as
complete on 2026-10-29.

The owner approved these AWS bindings and deviations on 2026-10-29. The
[AWS integration design](../design/aws-native-identity-integration-2313.md)
records them:

- Service principals prove identity with an AWS STS `GetWebIdentityToken`
  JWT. The binding is the account issuer and the IAM role ARN. Unlike a Google
  numeric subject, a role ARN is not immutable. A role created again with the
  same name keeps its principal. Provider binding subjects hold up to 2048
  characters, the IAM ARN maximum.
- Cognito sessions keep idle and absolute limits and an `AdminGetUser`
  recheck. A global sign-out made only in Cognito takes effect at the absolute
  limit.
- OpenFGA uses rotated password logins on AWS, because the released server
  cannot create RDS IAM tokens. An in-repo rotation function follows the
  documented Secrets Manager steps, because the AWS templates create Lambda roles
  without the required CI permissions boundary. A weekly Job copies the current
  credentials into named Secrets and restarts only the OpenFGA Deployments.
- Named operator principals assume the deploy role. IAM users need MFA. A
  Session Manager tunnel reaches the private EKS API. The CI deploy trust names
  exact deploy Environments and protected refs. The legacy administrator-user
  Terraform root is removed.

These choices add no token format, policy engine or credential broker. Source
delivery does not claim live AWS qualification.

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

The #2317 [preflight](../architecture/account-administration-preflight-2317.md)
and [coverage inventory](../architecture/account-administration-coverage-2317.md)
clarify the integration boundary: human-user lifecycle is not customer Account
lifecycle, deployment-wide settings are not customer policy, and invitation
acceptance is not identity resolution by email. Prepared adapters and catalog
entries do not prove mounted-operation coverage. Provider failure cannot become
an empty authorized result; transfer summaries must preserve blocked outcomes
and must not claim atomicity across independently committed resources.

PLAT-232 retains its UUID-only, four-field organization profile contract,
PATCH mask, owning validation, locked writer and strict field-name-only audit.
Its S1 session-only role/superuser rules do not survive S8 as either a grant or
a second veto: the canonical profile commands enforce `organization.read` or
`organization.update` with live principal policy and the credential ceiling.
Profile reads must not become authorization for pack, adapter, model-source or
workspace writes that previously borrowed the legacy org-admin helper. Those
effects use their own registered actions. Browser discovery distinguishes read
from edit authority and requires no staff flag or workspace membership.
The [PLAT-232 activation guidance](../architecture/integrated-identity-cutover-preflight-2321.md#plat-232-organization-profile-activation)
records the prepared credential/commit, evaluator-error, route, client and
cross-domain integration gaps; preparation alone is not activation evidence.

PLAT-233 activates the prepared workspace commands at the same S8 boundary.
`organization.manage_workspaces` authorizes creation; exact `workspace` actions
authorize discovery and lifecycle effects. Membership and multiple-owner facts
remain distinct from policy grants: transfer does not grant or revoke policy,
and neither full human nor service administrators require a legacy owner role.
Keep active defaults, ownerless creation, last-owner protection, authorized
archive/restore no-ops, strict audit and model-access invalidation without
deleting or rehoming bound ranges. Creation also needs confirmed SQL-owned
parent projection through the incumbent authorization boundary; structural
parent relations are not grants, and SQL/OpenFGA writes are not atomic.
The [workspace activation guidance](../architecture/workspace-lifecycle-preflight-1940.md#s8-activation-boundary-2321)
records the shared credential/commit, ancestry, error, parsing and live-route
gaps; the historical S1 role/session rules are not additional S8 vetoes.

Per-resource membership, ownership, policy and projection rejection must retain
earlier committed offboarding counts. The prepared summary reports all blocked
workspaces separately from confirmed missing-membership cases. Credential
revocation and rotation acquire the owner mutex before the token mutex, matching
identity lifecycle. Recovery-email changes require the owning human session;
an installation principal-administration grant alone cannot redirect recovery.
Standalone organization operation recovery derives its persisted account
ancestry and retains the journal's current-effect authorization checks.

The prepared OpenAPI TypeScript declaration follows the incumbent generated
schema analysis exclusion under the exact, owned, time-bounded ADR-003-R7
exception. DRF contract drift and TypeScript consumer checks remain mandatory;
handwritten administration code retains the ordinary analysis gates.

S8 is also the published v1 hard-cut boundary. The retirement manifest records
the removed user-keyed event staff, organization model-source-user, personal
workspace, workspace membership-role, and workspace-local authorization paths.
It also records the removed integer `workspace_id` and user/email policy fields.
Their replacements use stable principal UUIDs, explicit account ancestry, and
the mounted scoped authorization routes. Incompatible commands use explicit
principal, `account-workspaces`, event `staff-members`/ownership, range
`model-access`, or range `scope` paths. Retained operations keep their published
operation IDs; the cutover does not silently repurpose a removed path or field
as a compatibility adapter. Native scoped scenario discovery uses
`ctf/scenario-options`; the unusable unscoped `ctf/scenarios` path is retired.
Retained page, webhook and award creation commands preserve their published
HTTP 200 success contract.

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

Catalog management commands authenticate actual native bearer proof from a
private file through the existing API-token authenticator. Django staff flags,
actor names, credential references, workload IAM and maintenance mode cannot
authorize registration or conformance. Shipped content uses explicit installation
scope and the ordinary registration, conformance and publication services.
Registration admission precedes the atomic manifest batch; conformance and
publication follow commit with current proof and policy. Failed qualification
leaves native pending or unpublished content for exact retry.

Deployment schema Jobs do not initialize the application catalog. Catalog setup
follows verified native activation and application readiness. Without an actual
current proof, deployment reports catalog setup incomplete and leaves the active
application available for administrator login and credential issuance. It must
not create an implicit deployment grant or weaken the runtime gate to resolve
that prerequisite.

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

For an assigned CTF range, the CMS remote-access admission also asks the CTF
owning service to check the owner's retained participation and exact range
assignment. A ban, deleted participation or event end denies admission and
renewal even when the original credential and range policy remain valid. The
check uses the shared range service port. Independent operator access to
another owner's range remains subject to its own range policy; a participant's
ban cannot remove that operator grant. Temporary event credentials gain no new
range permission through this check.

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

Both the ADR guard and the standalone layer checker verify the repository's
actual per-symbol configuration in regression tests. Their Engine fixtures pin
`resource_scope_from_ids` and `resolve_range_scope_binding` alongside the existing
organization read seam. Keep both fixtures aligned with the documented allowlist;
these ancestry reads grant no additional workspace mutation or policy authority.

The deployment status probe installs only namespaces, service accounts,
configuration and secret references. Workload shutdown and autoscaler removal
run only when the probe requires identity cutover. Routine releases of an
activated installation do not enter this maintenance boundary.
The probe must complete and return a known state. A missing legacy cutover
table is an explicit pre-cutover state; database failure, timeout, image or
scheduling failure, and malformed or unknown status abort before workload
mutation. An activated result also requires the runtime binding check.

Permanent event deletion checks live `event.delete` authority and exposes
retained ancestry only inside its synchronous cleanup context. CMS validates
the exact event/range binding and commits generation-bound native cleanup
before CTF removes aggregate records. A failed queue commit preserves those
records. The retained cleanup cannot launch resources or change their generation.

An owner handoff can remove its initiator's administrative authority before
the final readback. Its retry requires the exact original live credential and
can confirm the matching admitted revoke by primary readback. This recovery
does not authorize or replay a provider write. New writes retain normal live
delegation checks.

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

### Prepared composition boundaries (#2317)

Dormant domain URL modules own their account administration routes. The
composition root imports the public `PolicyAdminView` admission/error base and
`AuthorizationMutationSerializer` operation projection for its two cross-domain
adapters. The layer checker permits only those named presentation symbols and
the exact tenancy journal/placement services. Domain persistence and private
helpers remain inaccessible to composition. No production URL or Django-admin
registration changes in S4.

## S7 native identity integration

Issue #2320 adds the released Cloud SQL Auth Proxy as one digest-pinned native
sidecar with a loopback SQL listener and automatic IAM renewal. The hardened
provisioner Job admits exactly that sidecar only when trusted deployment
parameters select native GCP SQL; image/argument/credential/mount/identity
substitution remains denied. Deterministic Job recovery also verifies it.
IAM connector/login bindings apply only to the installation instance. The
Guacamole client and deployment owner receive connector transport only; guacd
has its own KSA and no cloud identity. Existing WIF, operator and CI grants stay
additive. The directory duty contains only `firebaseauth.users.get`. The portal
and the delayed-work identities that recheck session credentials hold it.

OpenFGA runtime and administration use separate private authenticated listeners,
KSAs/GSAs and native database duties. Released PostgreSQL ACLs deny runtime
store/model/assertion mutation, schema ownership and migration-version writes;
the migration identity owns only the policy datastore. Runtime tuple writers
remain trusted. Deployment-owned bootstrap removes documented Cloud SQL runtime
role defaults, checks effective inheritance/ownership and fails closed on drift.
It preserves the separate qualified migration owner. Native IAM and admission
negative tests, released-server SQL tests and existing guards enforce this
boundary. The released-server lane publishes supplemental coverage for these
SQL duties to Sonar, alongside the platform fast-lane report. The existing
whole-package floor and changed-code coverage gate remain enforced; supplemental
service coverage does not replace either. Two historical permission migrations now quote the active database
name when granting/revoking CONNECT, correcting cold-install targeting without
changing their schema or intended duties. This does not redefine historical
account-policy invariants.

See the [operator guide](../ops/gcp-native-identity.md) and
[implementation contract](../technical/platform_infrastructure/gcp-native-identity.md).
Local tests do not claim live credential renewal or effective deployment IAM;
deployment-specific qualification belongs to the operator's live testing session
and is not a source-publication gate.


### S8 admission, retry and inventory enforcement

Issue #2321 applies prepare/commit admission to nested CTF mutations and
communication aggregates. Provider calls precede SQL locks; locked writers fence
local proof lifecycle and exact resource bindings. Scheduled dependency outages
retry within the release grace window. Durable authority recovery can use a fresh
administrator proof only after checking every permission required to replay the
exact original effect; original attribution remains in the journal.

Hierarchy creation returns a pending receipt after an uncertain publication.
Receipt retries retain object identity and validate the exact original declaration
and current parent authority. Independent descendant discovery does not require a
parent read grant. CTF dispatch stays deferred until its aggregate association is
recorded, and recovery cleanup retains native generation authority before pointer
replacement. Warm and cold reservations use Account then Workspace lock order;
claim activation and lease assignment commit together.

CTF commits each launch request UUID, immutable inputs and original non-secret
credential reference before CMS creation. Participant, spare and rebuild retries
reuse that request. CMS rejects conflicting input digests. A native creation
receipt retains cleanup custody even before dispatch. The aggregate resolver may
identify a reserved request for cleanup, but runnable operations require the exact
live participant, spare or recovery association. Current event administrators may
cancel an unrealized reservation or clean its recorded native generation; neither
path replaces the original proof for launching work.
Cleanup cancels retained intents under the event lock. CMS checks that same
locked intent before reservation and native creation. Cleanup includes unbound
requests, and releases their claims only after runtime absence is established.
Database guards preserve launch inputs, proof, pinned association and cancellation.

Tenant pack uploads reserve their exact storage cleanup key before sending bytes.
The uploader lease fences registration. Cleanup retains an unknown upload outcome
and repeats exact-key deletion after lease expiry; one absent read never settles
an upload that may still finish. Successful registration detaches this temporary
cleanup intent in the same transaction as native source custody.
Installer copies also retain unknown outcomes as pending finalization cleanup.
An age threshold or absent read does not establish that an interrupted copy ended.
Expired installer records remain exact-key tombstones for bounded repeat cleanup
of late staging writes. Installed bytes are removed only after cancellation or
installer retirement.
The existing CMS cleanup worker runs installer reconciliation on every pass,
independent of pending pack removals. Each reconciliation retains its own failure.

The API retirement guard accepts only exact ADR-backed query-parameter removals
and rejects reintroduction. Lease administration retains its published JSON and
form media types. JSON scalars remain strict; standard form parsing requires
all declared fields and rejects duplicate values before mutation.
The surface inventory test resolves the production admin registry and
WebSocket routes. Deployment checks extract one framed portal status result;
background node labeling receives no policy credential mount. These changes retain
the existing CI, policy journal, strict audit and architecture checks.

Maintenance validates workload inventories and waits for old application pods to
terminate before migration. The status probe receives the existing DNS,
metadata-server, Google API and native SQL network grants before its first run.
Broker retirement also precedes any restoration of broader egress policy;
maintenance applies its workload manifest after old application pods terminate.
Workspaces owns cutover verification, including memberships in already-bound
organizations. Legacy administrator roles without an equivalent policy mapping
block activation. Retained legacy spare owners block field removal until drained.

Tenant source registration verifies durable archive custody in the admitted
scope before any source creation or storage read. Destination content permission
alone cannot authorize a generic storage reference. Launch retry keys include
the explicit resource scope. Spare intents commit before their rows become
available to cleanup; cancellation fences all later creation. Claim release
requires verified runtime absence and an exact current-generation match.
Capacity release and claim settlement share one transaction. A failed release
retains the claim for retry. Cancelled consumed spares and rebuild replacements
remain cleanup targets while recovery changes participant pointers.
Organizer event paging exposes complete counts and browser continuation. An
organization UUID binds pending account-administration state to its original
parent.
The retained browser journeys and accessibility matrix use the native account
administration surface. Browser CI reuses the released-server OpenFGA harness
and explicit synthetic policy grants; legacy role rows do not grant access.

Temporary participant target discovery retains the event-only credential ceiling:
live participation and exact assignment admit the CMS and Engine projection.
Content discovery carries explicit scope and translates domain failures through
the API envelope. A collection excludes ineligible individual campaign targets,
but propagates credential and dependency failures. Early communication release
requires a session credential at the owning service boundary.

Pending handoffs retain an authorized settlement path after recipient
deactivation; new grants require an active target. Verified cleanup of the exact
old range may precede recovery repointing without admitting unrelated assignment
changes. Installation warm generations retain exact cleanup authority through
retirement; recorded origin, generation and verified absence govern release.
Ordinary pool retirement locks and rechecks unclaimed custody. Existing failed
or timed-out activation recovery uses the current owner's exact cleanup receipt
and rechecks current runtime state and deadline under the ledger lock. Healthy
customer claims remain outside pool retirement. Uncertain dispatch after native
creation retains its capacity draw until verified cleanup.
Warm lifecycle result application takes the generation ledger before the runtime
row, matching creation, claim and retirement, then rechecks the current operation.
An exact retained event handoff may settle its remaining recipient or previous
owner effects after deactivation, under current administrator delegation and durable transition
binding. This exception does not admit inactive actors or arbitrary new grants.

### Integration contract clarification

The [identity and lifecycle contracts](../technical/shifter_platform/identity-lifecycle-contracts.md)
define domain ownership, DFDs, lifecycle transitions and recovery obligations.
They retain the existing domain facades, authorization journal and Engine outbox.
They add no permission store, orchestration framework or provider service.

Warm replenishment policy cannot remove a retained cleanup obligation.
Activation recovery uses the claim timestamp, independent of earlier pool age.
Warm count limits require atomic database admission across concurrent workers.
Unknown cleanup retains its draw and requires a continuing recovery owner.
Launch, cancellation and failure settlement must share their target-before-intent lock order.
The launcher currently serializes provider submission under its target lock.
Any change to that boundary must preserve cancellation and late-submission protection.
The S8 proposal records remaining implementation and verification gaps against these contracts.

An immutable staff-assignment plan also retains its admitted subject through deactivation.
Current administrator delegation and the exact persisted effect govern settlement.
The rule is limited to unfinished, fully preflighted plans; new inactive grants remain denied.
This prevents a partial assignment from blocking all later event authority changes.

ADR-068 continues to park model-access sharing. The cutover closes its mounted
API and composition/selector entry points before data reads or effects; it does
not add native publisher storage or extend broker capability. An enabled setting
cannot unpark the feature. Keyless-direct and independently supported native
source/range controls retain their existing boundaries.

Notification recipient facts and scoreboard participation facts are projections,
not alternate permission providers. Notification subscriptions, replay and
delivery retain the actual credential and require current policy or exact live
participation. Historical creators, staff groups and user flags cannot bypass
those decisions. Public scoreboard publication remains an explicit exemption.

The communications console uses the same native boundary. Campaign reads admit
the complete target set; recipient details also require participant-management
authority on the selected event. Discovery checks current event authority before
pagination and returns an explicit account, organization or workspace parent.
The retained workspace-only response is a compatibility projection of that same
discovery, not a second authorization path. Parent names are disclosed only for
already-admitted event scopes through the owning Workspaces service. Revision,
release and cancellation attribution identify the actual principal and credential;
service actions never inherit a human contact's identity.
