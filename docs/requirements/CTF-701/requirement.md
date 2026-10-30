---
id: CTF-701
title: "Event Lifecycle"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:23.085872Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-701: Event Lifecycle

## Statement

The system shall manage CTF event states via a state machine with states: draft (initial setup), registration (accepting participants), active (competition in progress, flag submissions accepted), paused (temporarily halted, no submissions), ended (competition finished, results visible), cancelled (event terminated before completion), and archived (historical record). Valid transitions shall be enforced: draft->registration, registration->active, active->paused, paused->active, active->ended, paused->ended (automatic end at the scheduled end time only), ended->archived, draft->cancelled, registration->cancelled, active->cancelled, paused->cancelled. No backward transitions past ended.

## Rationale

Event lifecycle state controls what actions are available at each phase, you cannot submit flags before the event starts or register after it ends. CTFd manages event state. For Shifter, event state also gates range provisioning and teardown, making correct state management critical for infrastructure cost control.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (EventStatus enum - defines event lifecycle states)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Event service - state transition functions (schedule, activate, complete, cancel))
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#518` (CTF-701: Event Lifecycle)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/event.py` (CTFEvent model - event entity with status field)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_event_lifecycle.py` (Event lifecycle transition tests - all 10 valid transitions + invalid transition rejection)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py::SCHEDULED_END_TRANSITIONS` (Scheduled-end-only paused->ended edge, validated by validate_transition)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/_lifecycle.py::complete_event` (Automatic end at the scheduled end time also ends a paused event)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_organizer_lifecycle_alerts.py` (Paused event ends automatically; manual end of a paused event stays refused)
- IMPLEMENTS → GITHUB_ISSUE `528` (Issue #528 - automatic end of a paused event)
