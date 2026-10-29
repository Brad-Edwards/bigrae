---
id: CTF-1402
title: "Theming"
status: ACTIVE
type: NON_FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:24.378268Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-1402: Theming

## Statement

CTF events could support configurable branding including event logo, color scheme, and description. Branding configuration shall be per-event. Visual customization shall use per-event branding data that the platform user interface applies, not a standalone theming engine.

## Rationale

Event branding enables differentiated event experiences, important for events co-hosted with external organizations or customers. The React SPA (ADR-029) applies the per-event branding fields, so no separate theming infrastructure is needed.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/event.py` (CTFEvent logo_url and theme_color branding fields)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/EventHomePage.tsx` (Participant event home applies the event theme color and logo)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#639` (CTF-1402: Theming)
