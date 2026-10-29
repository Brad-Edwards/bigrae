---
id: CTF-1403
title: "Internationalization"
status: DRAFT
type: NON_FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:24.411313Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-1403: Internationalization

## Statement

The CTF module could support a multi-language UI through the platform's language support. Translatable strings shall be externalized for Django-rendered text (gettext, ADR-016) and for the React SPA (ADR-029). Participants could select their preferred language. The system shall support at minimum English and one additional language.

## Rationale

Multi-language support enables running events for participants across different regions and language backgrounds. Django gettext covers server-rendered text. The React SPA needs its own translation layer, which #91 tracks. No custom translation infrastructure is required.

## Traceability

- CONSTRAINS → CONFIG `shifter/shifter_platform/config/settings.py` (LocaleMiddleware, USE_I18N, LANGUAGE_CODE and LOCALE_PATHS are configured; English is the only catalog)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#91` (Gap: language selection and SPA translation for the whole platform)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#640` (Closed as a duplicate of #91)
