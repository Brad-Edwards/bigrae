---
id: CTF-805
title: "Email Templates"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.425637Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-805: Email Templates

## Statement

CTF email notifications shall use the platform's email templating and delivery service (PLAT-103). Default templates shall be provided for all CTF notification types (invitation, credentials, reminder, announcement, provisioning failure). Organizers could optionally customize templates per event. Template variables shall include participant name, event name, event URL, and dates.

## Rationale

Email delivery is a platform capability. CTF defines its notification types and templates but delegates rendering and delivery to PLAT-103 rather than implementing its own email pipeline.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (CTF Notification Service: lifecycle notices staged with their notice kind (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/adapters/email.py` (Email adapter: selects the trusted pair by notice kind; participant name, event name, dates and portal URL; delegates delivery to shared.email (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/campaigns.py` (Organizer campaigns are announcements; trusted notices carry their kind (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums_communication.py` (Closed NoticeKind set (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/communication.py` (CommunicationCampaign.notice_kind and its check constraint (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/migrations/0082_notice_kind_retire_email_template.py` (Adds notice_kind; drops the retired per-event override table (#580))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/templates/ctf/email/notice/` (Default notice template pairs: announcement, reminder, cleanup, results, range ready, range failure (#580))
- IMPLEMENTS → DOCUMENTATION `docs/design/ctf-ledger-email-template-boundaries-580.md` (Ledger email template design and override retirement (#580))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_email_adapter.py` (Per-kind default templates, escaped values, snapshot-event facts, unsafe SITE_URL, forged kind rejection (#580))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification.py` (Notification Service Tests)
- IMPLEMENTS → GITHUB_ISSUE `580` (CTF-805: apply email templates to communication ledger email)
