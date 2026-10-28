---
id: CTF-007
title: "Event Management"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:21.124447Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-007: Event Management

## Statement

The system shall manage CTF events as time-bound competitions with a well-defined lifecycle, enforced state transitions, and configurable timing that controls when participants can register, compete, and view results.

## Rationale

Events are the top-level organizing unit for CTF competitions. Event state controls when participants can register, submit flags, and view results. In Shifter, event state also controls range provisioning lifecycle, an event in the wrong state can leave expensive cloud resources running. This tight coupling between event state and infrastructure cost is Shifter-specific and the primary reason state management must be rigorous.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Event service - lifecycle transitions (schedule, activate, complete, cancel))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/submission.py` (Submission service - submit_flag enforces event must be ACTIVE to compete)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (CTF enums - EventStatus lifecycle states and terminal status definitions)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_models.py` (Model tests - CTFEvent lifecycle properties, validation, soft delete, scheduled tasks)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF models - CTFEvent lifecycle fields, time-bound configuration, registration_deadline)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_participant.py::TestRegistrationDeadlineEnforcement` (Tests for registration deadline enforcement in invite_participant and bulk_import_participants)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Scoring service - get_scoreboard() with freeze_at cutoff for results visibility timing)
