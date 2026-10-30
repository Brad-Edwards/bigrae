---
id: CTF-1002
title: "Automated Range Spinup"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.775033Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-1002: Automated Range Spinup

## Statement

The system should automatically trigger range provisioning for all registered participants at a configurable time before the event start (for example 30 minutes before). The spinup task shall use throttled provisioning to avoid overwhelming infrastructure. If provisioning is not complete by event start, the system shall continue provisioning while the event runs and notify organizers of the delay.

## Rationale

Ranges take 10-20 minutes to provision. If provisioning starts at event start time, participants wait with nothing to do. Pre-provisioning ensures ranges are ready when the competition begins. Automation eliminates the need for organizers to manually trigger provisioning at the right time.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Schedules SPIN_UP_RANGES task at event.get_spinup_time() when event transitions to SCHEDULED)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (Configurable range_spinup_minutes field (default 30, 0-1440) and get_spinup_time() method computing event_start - spinup_minutes)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/` (Throttled provisioning - spreads requests across spinup_window_seconds, delay clamped [5,120]s, supports shutdown_check for graceful abort, continues on per-participant failures)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/management/commands/run_ctf_scheduler.py::_handle_spin_up_ranges` (SPIN_UP_RANGES task handler - dispatches to provision_event_ranges_throttled with spinup_window computed from range_spinup_minutes)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/provision.py::provision_event_ranges_throttled` (Throttled participant range provisioning used by scheduled spin-up tasks)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_range.py` (Range service tests - scheduled/throttled event range provisioning and participant roster behavior)
- IMPLEMENTS → GITHUB_ISSUE `539` (Issue #539 - automated coverage and Ground Control TESTS trace links for active CTF requirements)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/_organizer.py::alert_if_ranges_not_ready` (Organizer alert with cohort counts when ranges are not ready at the scheduled start)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/automation.py::run_automatic_start` (Checks range readiness at the scheduled start, also when the start is refused)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/management/commands/run_ctf_scheduler.py::_handle_spin_up_ranges` (Spin-up yields to due work and resumes as a continuation over the time left before start)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/tasks.py::get_provision_progress` (Readiness counts over the eligible registered cohort; ready needs a current assignment)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/` (Organizer recipient kind, release, worker fence and organizer inbox (ADR-065-R4))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/admin/OrganizerAlertsCard.tsx` (Monitoring page Alerts tab for organizer alerts)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_organizer_lifecycle_alerts.py` (Late-range alert counts, cohort, refused start and spin-up yield)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_organizer_alerts.py` (Organizer recipients, policy, worker fence, inbox and console isolation)
- TESTS → TEST `shifter/shifter_platform/frontend/src/features/ctf/admin/OrganizerAlertsCard.test.tsx` (Alerts tab list, read and dismiss)
- IMPLEMENTS → GITHUB_ISSUE `528` (Issue #528 - organizer alerts for late ranges and automatic start and end)
