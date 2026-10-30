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
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/announcement.html` (Announcement notice email template (HTML) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/announcement.txt` (Announcement notice email template (text) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/range_ready.html` (Range readiness notice email template (HTML) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/range_ready.txt` (Range readiness notice email template (text) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/range_failure.html` (Provisioning failure notice email template (HTML) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/range_failure.txt` (Provisioning failure notice email template (text) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/results.html` (Final results notice email template (HTML) (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/results.txt` (Final results notice email template (text) (#580))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification.py` (Notification Service Tests)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#664` (CTF-801: Email Notifications)
