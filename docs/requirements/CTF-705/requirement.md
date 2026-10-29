---
id: CTF-705
title: "Registration Deadline"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.224185Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-705: Registration Deadline

## Statement

The system should support configuring a registration deadline separate from the event start time. The registration deadline shall default to the event start time if not explicitly set. After the deadline, self-registration shall be closed but organizers shall retain the ability to manually add participants. The system shall display the registration deadline to prospective participants.

## Rationale

Organizers often need to know participant counts before an event starts to provision the right number of ranges. A registration deadline 24-48 hours before start gives organizers time to prepare infrastructure. CTFd supports registration cutoffs. For Shifter, advance registration is critical because range provisioning takes time.

## Traceability

- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#661` (CTF-705: Registration Deadline)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/event.py` (CTFEvent.registration_deadline and effective_registration_deadline, which defaults to event_start)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/public_registration.py` (Public registration shows the effective deadline and closes after it)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_public_registration.py` (Deadline-aware public projection and locked deadline recheck)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_participant.py` (Organizer import is still allowed after the deadline)
