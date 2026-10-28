---
id: CTF-012
title: "API & Integration"
status: ACTIVE
type: INTERFACE
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:21.306541Z
updated_at: 2026-03-26T06:34:18.550993Z
---

# CTF-012: API & Integration

## Statement

CTF operations should be exposed as REST API endpoints within the existing Shifter platform API, using the platform's authentication (PLAT-102) and following existing API conventions. The API enables automation, external integrations, and custom tooling built on top of the platform.

## Rationale

An API enables custom scoreboards, automated challenge deployment, integration with chat platforms for solve announcements, and programmatic event management. For Shifter, the API also enables future integration with PANW internal tools. CTF API endpoints are part of the platform API, not a separate service.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/urls.py` (CTF URL configuration - API route definitions)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#626` (CTF-012: API & Integration)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/serializers/communication.py` (Bounded read-only DRF projections for the participant inbox and organizer campaign summary (ADR-051, #2048))
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#2048` (API contract for scoped communications: bounded serializers and the CTFError -> shared.api.errors mapping)

- IMPLEMENTS → CODE `shifter/shifter_platform/ctf/api/organizer/communication.py`
- IMPLEMENTS → CODE `shifter/shifter_platform/ctf/api/communication_inbox.py`
- IMPLEMENTS → CODE `shifter/shifter_platform/ctf/services/communication/admission.py`
- IMPLEMENTS → CODE `shifter/shifter_platform/ctf/services/communication/cutover.py`
- IMPLEMENTS → CODE `shifter/shifter_platform/shared/api_tokens/scopes.py`
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_rest.py`
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_inbox.py`
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_cutover.py`
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_admission_postgres.py`
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#2100`
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/operation_policy.py` (ADR-066 S5 action and scope locator contract for published CTF operations; S8 activates administration authority)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_authorization_operation_map.py` (Published OpenAPI operation and browser route coverage)
- IMPLEMENTS → CODE `shifter/shifter_platform/ctf/api/serializers/communication_console.py` (Closed query inputs and read-only console projections (#2052))
- IMPLEMENTS → CODE `shifter/shifter_platform/frontend/src/api/ctfCommunications.ts` (One parameterized submit path for the console and single-event announcements (#2052))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_communication_console_api.py` (Exact read/write scopes, CSRF, foreign identifiers, cross-workspace denial (#2052))
- IMPLEMENTS → GITHUB_ISSUE `2052` (Issue #2052 - console API routes and scopes)
