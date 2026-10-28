# Account administration implementation

ADR-066 S4 composes policy-aware services with DRF, the existing policy journal,
Django locks/constraints, strict shared.audit writes, signed invitations and the
existing React query client. S8 mounts these domain routes in the production
API composition. `config.account_admin_urls` also remains a standalone test
and schema URLConf. Domain routing modules own their paths. The implementation introduces no
new authentication provider, policy evaluator, audit store or environment key.

Organization/workspace memberships retain legacy user/role rows alongside
principal-only rows with an empty role. Named database constraints prohibit
mixed identity shapes. `WorkspaceOwnership` is a separate, unique-per-principal
fact, allowing multiple owners. Owning services lock the parent row to preserve
membership, quota and last-owner invariants. Migration 0020 refuses rollback
once principal facts or invitation provenance exist; empty preparation can be
reversed. S8 maps historical facts explicitly rather than activating guessed
membership or policy.

The cutover inventories user-keyed organization and workspace memberships even
when their organization already has an account. It converts supported roles to
principal facts and the recorded policy intent, then rejects activation if any
legacy user/role row or planned membership disposition remains incomplete.
Legacy workspace administrators require an explicit policy disposition before
conversion: the native administrator grant also permits ownership transfer, so
substituting it would elevate the old role. Existing SQL account ancestry also
determines resource conversion for already-bound organizations and workspaces.

CTF migration `0071_remove_ctfsparerange_owner_user` refuses to remove ownership
while any legacy spare row remains, including retained consumed or retired rows.
An upgrade must drain and explicitly disposition those generations under the
previous release first. The migration preserves the spare rows and their owner,
request and range references on refusal; it neither guesses principal custody
nor deletes cleanup evidence. Fresh installations with an empty spare pool can
continue through the schema change.

CTF migration `0076_event_authority_effect_plan` also refuses an upgrade with
unfinished legacy staff or ownership transitions. Settle those transitions on
the previous release before migration. It does not infer missing effect plans.
Confirmed history remains intact; new pending transitions require an immutable,
versioned plan. Database triggers protect that declaration from later changes.

Owning services revalidate the active principal, immutable credential ceiling,
exact registered action, SQL-owned ancestry and live evaluator. Provider calls
precede SQL row locks; locked writers compare the authorized ancestry and
eligibility again. An incomplete batch or evaluator error cannot produce a
partial list/count. Administrative account/organization/workspace reads belong
to the declared administrator action set. Self-leave is a separately registered
ordinary action, subject to live policy and the last-owner invariant.

The CMS service composes source/destination tenancy proofs with Engine's
expected-owner/ancestry comparisons. Ownership preserves existing range and VPN
constraints. Scope changes update CMS request, CMS projection, Engine projection
and quota reservation in one transaction. Destination capacity is admitted
before releasing the source. Reopened historical reservations support moving
back without bypassing quota. Strict audit failure rolls effects back.

Prepared Django-admin classes disable add/change/delete and bulk mutation.
`PolicyAdminSite` admits active human browser sessions without a staff-flag requirement;
registered model admins authorize reads and filter before counting. Canonical
DRF/browser commands replace direct lifecycle, invitation and quota writes.
S8 installs the policy-aware site and the retained registrations.

The prepared OpenAPI artifact is derived from the same DRF serializers as the
prepared routes. Method docstrings appear in this artifact. After changing them,
regenerate both this artifact and the ordinary API contract. Generate the
prepared artifact and its client with:

```bash
cd shifter/shifter_platform
TESTING=1 TEST_DB_BACKEND=sqlite uv run python manage.py spectacular \
  --urlconf config.account_admin_urls --format openapi-json \
  --file openapi/account-admin-prepared.json --validate --fail-on-warn
cd frontend
./node_modules/.bin/openapi-typescript ../openapi/account-admin-prepared.json \
  -o src/api/account-admin-schema.d.ts
```

The ordinary API contract describes the mounted production routes. The
SPA routes account and platform pages through action-derived navigation hints;
individual presentation comes from the principal/account context. Parent scope
keys reset child drafts, query keys contain exact paths, and policy mutations
reuse the incumbent frozen idempotency and reconciliation behavior.

See [the operation inventory](../../architecture/account-administration-coverage-2317.md)
for baseline replacements and tests. The released OpenFGA/PostgreSQL suite
verifies human/service hierarchy discovery using the declared administrator
credential ceiling and proves that narrowed credentials cannot edit descendants.
Existing journal suites cover delegation, descendant proof, deny and recovery.

Policy evaluator failure returns a retryable unavailable response. It does not
record an explicit policy denial or consume a command's retry identity. The
event service checks the complete delegation plan before recording a pending
staff change. See [identity and lifecycle contracts](identity-lifecycle-contracts.md)
for the policy publication sequence, retained recovery and trust boundaries.

Offboarding preserves the command’s expected departing owner through both legacy
and prepared per-range writers, including Engine comparison; an intervening
owner change is blocked. Named active-range collisions roll back that resource
and retain the earlier successful counts. Terminal status convergence locks the
range projection and releases the current persisted workspace reservation, even
when the consumer loaded its instance before a scope move. The integer-user
workspace transfer compatibility helper uses principal ownership facts and
leaves legacy role rows unchanged.

Known per-resource policy, membership, ownership and projection rejections retain
earlier committed offboarding effects. The prepared `workspaces_blocked` count
covers all workspace rejections; the incumbent missing-membership count retains
its narrower meaning. Archived ownership is reported as blocked after an exact
policy check, without preventing transfers of eligible resources. Standalone
organization operation recovery derives the
persisted account ancestry before the journal reauthorizes its recorded effect.

Personal credential commands lock the owner before the token, matching lifecycle
revocation. Metadata email changes require that owner's human session under the
same locked user boundary. The administrative grant alone cannot redirect a
password reset to another recipient.

The exact generated TypeScript declaration follows the incumbent schema analysis
exclusion recorded as an owned, time-bounded ADR-003-R7 exception. DRF drift,
TypeScript compilation and consumer tests remain enforced; handwritten code is
fully analyzed.

## Discovery and publication recovery

`GET /api/v1/administer/organizations/?integration=adapters` discovers
organizations with `organization.manage_adapters`. The `model_sources` value
selects `organization.manage_model_sources`. Each result requires the exact
SQL ancestry, credential ceiling and live decision. Parent account-read
permission is not required. Counts and pages include only permitted rows.
The bootstrap permission hints use the same organization decisions alongside
installation actions.

Account, organization and workspace creation persist their parent-edge intent
before provider I/O. An uncertain publication returns HTTP 202 with `uuid`,
`publication_receipt` and `publication_state: pending`. The browser retains the
original declaration and retries it with that receipt. Current creation authority,
exact parent, name, account kind and individual owner must still match. A retry
resumes the same object, including after the publication has completed. It cannot
create a replacement or reinterpret the receipt. After restoring the policy
service, an operator can also run:

```bash
python manage.py reconcile_hierarchy_publications --limit 100
```

Repeat until no pending publications remain. Recovery preserves object IDs,
reuses the recorded provider binding and validates current SQL ancestry. It
does not create another account or infer administrator grants.


`GET /api/v1/administer/scopes/?kind=organization` and `kind=workspace` discover
independently readable descendants. Results carry their non-secret account and
organization UUIDs for routing; a parent read grant is not required or inferred.
The context and bootstrap hints include these grants. Each subsequent resource
read and mutation still checks its own exact policy action.
