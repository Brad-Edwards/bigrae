---
id: CTF-601
title: "Participant Registration"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:22.755543Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-601: Participant Registration

## Statement

The system shall support associating platform users with CTF events via a registration flow. Registration shall collect a display name and email address, and create or link a platform user account. Registration shall be available during the registration window (between event creation and registration deadline). The system shall enforce unique email addresses per event. Registered participants shall receive a confirmation with event details. The CTF layer uses the platform's user management (Management layer) for account creation, not its own user store.

## Rationale

Self-registration is the primary onboarding path for CTF participants. CTFd supports open registration with configurable fields. For Shifter, registration must be low-friction since consultants register from various locations and devices, often at the last minute before an event.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF models - CTFParticipant with unique email constraint and CTFEvent with registration_deadline)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/bulk_import.py` (Participant service - bulk_import_participants)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (Notification service - send_invitations, email rendering (no registration confirmation))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/forms/` (CTF forms - CTFParticipantForm collects name and email (organizer-facing))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_auth.py` (Auth tests - TestCTFRegisterView magic link token validation)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification.py` (Notification service tests - invitation sending, credentials, rendering)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#650` (CTF-601: Participant Registration)
