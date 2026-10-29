---
id: PLAT-103
title: "Email Templating and Delivery Service"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-26T06:09:28.301270Z
updated_at: 2026-10-29T00:00:00Z
---

# PLAT-103: Email Templating and Delivery Service

## Statement

The platform shall provide a shared email templating and delivery service. Templates shall support variable substitution for dynamic content (user name, event details, URLs, dates). Default templates shall be provided for common notification types. The service shall report delivery success only for an observed backend acceptance. CTF communications shall be delivered asynchronously through the durable communication ledger and its supervised worker, with bounded retry. Workspace invitation and password-reset messages shall be sent once after the triggering transaction commits, within a bounded timeout, and shall report their outcome. Failed delivery shall be logged without recipient data and shall not roll back the triggering operation.

## Rationale

Multiple platform features (CTF notifications, range readiness alerts, account management) need to send templated emails. A shared email service avoids each feature implementing its own email rendering and delivery logic. Consistent email formatting and reliable delivery are platform-wide concerns.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/email.py` (Shared email templating and transport seam (render_template, attempt_email, delivery_available; ADR-071))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/notification/` (CTF notification service: template rendering through shared.email; non-secret notices stage ledger campaigns)
- TESTS → TEST `shifter/shifter_platform/tests/shared/test_email.py` (Shared email seam: rendering, one bounded attempt, closed classification, timeout options, Message-ID, receipt, unavailable backend)
- IMPLEMENTS → GITHUB_ISSUE `581`
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification.py` (CTF notice staging and retired-sender tests)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_notification_helpers.py` (CTF notification helper/render tests (PLAT-103))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/config/_email.py` (EMAIL_DELIVERY_AVAILABLE: non-delivering backends unavailable outside development and test (ADR-071))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/communication/adapters/email.py` (Asynchronous durable CTF email through the communication ledger worker (ADR-071))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/workspaces/services/_invitations.py` (Bounded after-commit invitation send with persisted, generation-fenced outcome (ADR-071))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/management/password_reset.py` (Bounded after-commit password-reset send that reports a non-accepted outcome (ADR-071))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_email_adapter.py` (Durable CTF email delivery through the ledger worker)
- TESTS → TEST `shifter/shifter_platform/tests/workspaces/test_invitations.py` (Invitation delivery outcome, fencing, and unavailable transport)
- TESTS → TEST `shifter/shifter_platform/tests/management/test_password_reset_delivery.py` (Password-reset delivery outcome and API status mapping)
