---
id: CTF-602
title: "Participant Invitation"
status: DEPRECATED
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:22.794291Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-602: Participant Invitation

## Statement

Historical requirement, retired by ADR-049. Organizers add participants with immediate account provisioning, and the old invitation endpoints return 410. CTF-006 and public registration (#2157) describe current onboarding. The historical statement below does not describe a current endpoint.

The system shall support organizer-initiated participant invitations via the platform's email service (PLAT-103). Invitations shall contain a unique link that pre-registers the recipient for the event. Invited participants shall be able to accept or decline. The system shall track invitation status (pending, accepted, declined, expired). Organizers shall be able to resend or revoke invitations.

## Rationale

Invitation-based registration is essential for private or enterprise CTF events where not everyone should be able to self-register. For Shifter, organizers need to invite specific consultants or customer contacts to controlled events rather than opening registration publicly. (CTFd supports invite-only events.)

## Traceability

- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/lifecycle.py` (Historical: add_participant and resend_login_info)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (Historical: invitation sending, now retired by ADR-065)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (Historical: invite token fields, removed by the isolated-account migration)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Historical: invitation registration views)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (Historical: invitation participant states)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.html` (Historical: invitation email HTML template)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.txt` (Historical: invitation email text template)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/urls.py` (Historical: invitation routes)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#651` (Closed: conflicts with ADR-049)
