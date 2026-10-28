---
id: CTF-801
title: "Email Notifications"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.290765Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-801: Email Notifications

## Statement

CTF events should send email notifications to participants for key milestones using the platform's email service (PLAT-103). Notification triggers include: registration confirmation, event cancellation, range readiness, provisioning failure, and event completion with final results. Emails shall be sent asynchronously and shall not block the triggering action. Failed delivery shall be logged but shall not affect event operations.

## Rationale

Email delivery is a platform capability. CTF defines when and what to send but delegates delivery to PLAT-103. This avoids CTF implementing its own email transport layer.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (CTF Notification Service)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF Models (CTFNotification, CTFParticipant))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (CTF Views (notification endpoints))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/signals.py` (CTF Signals (range_status_changed))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (CTF Enums (NotificationType, NotificationStatus))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (CTF Event Service (cancel_event))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/credentials.txt` (Credentials Email Template (text))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/announcement.txt` (Announcement Email Template (text))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/reminder.txt` (Reminder Email Template (text))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.html` (Invitation Email Template (HTML))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/announcement.html` (Announcement Email Template (HTML))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/reminder.html` (Reminder Email Template (HTML))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/invitation.txt` (Invitation Email Template (text))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/credentials.html` (Credentials Email Template (HTML))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification.py` (Notification Service Tests)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#664` (CTF-801: Email Notifications)
