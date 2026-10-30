---
id: CTF-804
title: "Scheduled Notifications"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:23.392764Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-804: Scheduled Notifications

## Statement

The system could support scheduling notifications for future delivery at a specified date and time. Scheduled notifications shall support the same content as announcements. The scheduler shall process notifications within one minute of the configured time. Organizers shall be able to cancel scheduled notifications before delivery.

## Rationale

Scheduled notifications enable organizers to plan communications in advance, event reminders the day before, mid-event encouragement, or time-remaining warnings. Without scheduling, organizers must be online at the exact moment to send communications, which is unreliable for events that span multiple timezones.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/scheduling.py` (schedule_declaration schedules a communication; run_release_communication_task releases it when due)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/management/commands/run_ctf_scheduler.py` (RELEASE_COMMUNICATION task handler; the scheduler polls every 30 seconds)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/lifecycle.py` (cancel_campaign stops unreleased work)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/admin/communications/ComposeCommunication.tsx` (Organizers send now or schedule)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_scheduling.py` (Scheduling, due release, expiry, revoked actor and early release)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_lifecycle.py` (Campaign cancellation stops unclaimed work)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#667` (CTF-804: Scheduled Notifications)
