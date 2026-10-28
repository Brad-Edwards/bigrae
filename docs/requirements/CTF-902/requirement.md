---
id: CTF-902
title: "Range Lifecycle Management"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:23.499978Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-902: Range Lifecycle Management

## Statement

The system shall manage range instance lifecycle states tied to event state: provision when event becomes active, keep running during active/paused states, and destroy when event ends or is cancelled. Organizers shall be able to manually start, stop, restart, or destroy individual participant ranges. The system shall handle provisioning failures with configurable retry logic and organizer notification upon failure. Range state shall be visible to both the participant and organizer.

## Rationale

Range lifecycle must be tightly coupled to event lifecycle to prevent orphaned resources. Cloud VMs cost money every minute, ranges that outlive their event waste budget. CTFd has no infrastructure concept. Shifter must ensure ranges are created when needed and destroyed when done, with manual overrides for troubleshooting.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/` (Range lifecycle service (provision, destroy, cleanup, status))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Event lifecycle service (scheduled tasks for spin-up/cleanup tied to event states))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/management/commands/run_ctf_scheduler.py` (CTF scheduler - executes SPIN_UP_RANGES, CLEANUP_RANGES, EVENT_START, EVENT_END tasks)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (CTF views - organizer manual provision/destroy APIs, participant/organizer range status views)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/bridges.py` (CTF bridges - stop/start range operations via CMS services)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (Organizer notification on provisioning failure)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_range.py` (Range service tests - provision, destroy, status, cleanup)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#542` (CTF-902: Range Lifecycle Management)
