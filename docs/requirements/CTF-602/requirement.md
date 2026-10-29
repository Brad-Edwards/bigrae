---
id: CTF-602
title: "Participant Invitation"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:22.794291Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-602: Participant Invitation

## Statement

The system shall support organizer-initiated participant invitations via the platform's email service (PLAT-103). Invitations shall contain a unique link that pre-registers the recipient for the event. Invited participants shall be able to accept or decline. The system shall track invitation status (pending, accepted, declined, expired). Organizers shall be able to resend or revoke invitations.

## Rationale

Invitation-based registration is essential for private or enterprise CTF events where not everyone should be able to self-register. For Shifter, organizers need to invite specific consultants or customer contacts to controlled events rather than opening registration publicly. (CTFd supports invite-only events.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/lifecycle.py` (Participant service - add_participant and resend_login_info)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (Notification service - send_invitations, email rendering, magic link URL builder)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFParticipant model - invite_token, invite_token_expires, status, is_invite_valid)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Views - ctf_register (magic link auth), organizer invite/resend APIs, admin participant views)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (ParticipantStatus enum - tracks INVITED/REGISTERED/ACTIVE/COMPLETED/DISQUALIFIED (missing DECLINED/EXPIRED))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.html` (Invitation email HTML template with magic link)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.txt` (Invitation email plain text template with magic link)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/urls.py` (URL routing for registration, invite send, resend-invite endpoints)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#651` (CTF-602: Participant Invitation)
