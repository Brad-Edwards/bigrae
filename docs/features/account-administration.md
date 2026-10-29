# Account administration

The account administration components and APIs use the ADR-066 principal and
policy model. The source composition mounts account mapping, routes,
navigation, invitation acceptance and the policy-aware Django admin site.
This describes source behavior; it does not claim a live deployment.

An individual account opens directly, without organization or workspace
selectors. Team and enterprise accounts contain real organizations and
workspaces. Administrators can search permitted accounts/workspaces, create
subdivisions, edit names and organization contact settings, inspect quotas,
configure quota/egress policy, archive and restore workspaces, and manage
principal memberships, invitations and owners. Default workspaces cannot be
archived; archival preserves bound ranges.

Membership and ownership record facts. Permissions are assigned separately
through scoped groups and policies. Several workspace owners may exist; the
last owner cannot be removed or leave. A transfer requires an existing active
member as destination. Invitations use the existing signed email ceremony and
require the recipient's freshly verified identity. Acceptance creates membership
without granting permissions.

Platform administration provides user lifecycle and profile management,
password-reset delivery, independent resource transfers for offboarding,
credential administration, lease settings and audit history. A valid service
principal can use the administrative API without impersonating a human.
Personal token issuance still requires its owner's authenticated browser session
and the established proof and CSRF ceremony.

Recovery-email changes require the account owner's own browser session.
Administrators can edit other profile metadata and request password resets,
but cannot change another user's recovery recipient.

Range scope moves require authority in both source and destination scopes,
owner eligibility and any applicable destination quota capacity. The server moves the
persisted ancestry and active reservation together. Domain-owned aggregates
cannot be moved independently. Offboarding reports successful and blocked
resources separately so blocked resources are not silently reassigned.

All controls reauthorize on the server. A forbidden or unavailable scope is not
an empty collection, and an uncertain policy write must be reconciled through
its recorded operation rather than repeated as a new command. Historical audit
records remain installation-wide; they are not reconstructed as customer audit
records from current memberships or resource ancestry.

Adapter and model-source organization choosers use the corresponding
organization permission. They do not require permission to read the parent
account. Account administration also lists organizations and workspaces granted
to you directly. Select one to administer it without selecting its parent account.

If creation reports that access setup is pending, use **Retry access setup**.
The retry keeps the same object and original declaration. An operator can also
resume its saved publication. Do not submit a separate creation for that object.
