---
id: CTF-014
title: "Customization & Extensibility"
status: ACTIVE
type: NON_FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:21.376085Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-014: Customization & Extensibility

## Statement

The platform could support extensibility and customization for CTF functionality including: custom challenge types and scoring modes registered as Django apps, configurable event branding (logo, colors, description) via Django's template system, and multi-language support via Django's built-in i18n framework. The CTF layer shall not build a standalone plugin system, theming engine, or translation infrastructure.

## Rationale

Extensibility enables the platform to serve diverse use cases without core code changes. Django's app architecture, template system, and i18n framework already provide these extension points. Custom challenge types are Django apps registered with CTF. Event branding is template configuration. Translations use Django's standard gettext workflow.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/extensions.py` (Extension points: register_flag_validator and register_scoring_strategy for external Django apps)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/event.py` (CTFEvent logo_url and theme_color branding fields)
- CONSTRAINS → CONFIG `shifter/shifter_platform/config/settings.py` (Django settings - USE_I18N=True but no LOCALE_PATHS/LANGUAGES configured)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#627` (CTF-014: Customization & Extensibility)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/communication_contracts.py` (Closed, versioned content/audience/trigger/channel profiles: the CommunicationIntent extensibility seam extended by application change, not a plugin/theme/translation engine (ADR-051, #2048))
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#2048` (Communication extensibility realized as a closed seam that honors the shall-not-build-a-plugin-system constraint)
