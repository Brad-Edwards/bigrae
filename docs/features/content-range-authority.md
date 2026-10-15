# Content, ranges and continuing access

The S6 service integration is prepared for the coordinated S8 authorization
activation. Existing browser behavior changes at that cutover.

Human and service principals can administer content and ranges when their
current permissions and credential limits allow the operation. A service acts
as its own identity; its creator or contact does not supply permissions.

Content, installers, source credentials and preparation results belong to an
explicit customer scope. Publishing an operator catalog entry does not publish
customer credentials or private preparation material. Account-level ranges
have no synthetic organization or workspace. Service-owned ranges use the
tenant lease policy and the limits configured for their actual scope.

Revoking a credential prevents further authorized launches, retries and access.
Queued work retains the original credential limits, so later grants cannot
broaden a previously accepted operation. Authorized ownership and scope moves
preserve range identity and history. Required cleanup retains explicit authority
for the accepted resource generation.

Range and device remote sessions recheck the original credential and resource
permission while connected, including while idle. Denial or an authority-service
outage closes the session through its native transport. Termination takes a
finite interval; the tested fixture bounds and deployment prerequisites are
documented in the [technical guide](../technical/shifter_platform/content-range-authority.md).
An already delivered download or upload URL remains usable until its provider
expiry; revocation prevents new issuance and finalization.
