---
id: CTF-004
title: "Scoreboard & Rankings"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:21.012281Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-004: Scoreboard & Rankings

## Statement

The system shall present a live scoreboard that ranks participants by score, provides visibility into competition progress, and supports organizer controls over what ranking information is displayed and when.

## Rationale

The scoreboard is the primary competitive interface, participants check it constantly to gauge their standing. Scoreboard freeze near event end maintains suspense. A polished, real-time scoreboard is essential for engagement. The scoreboard is delivered as a CTF view within Mission Control's UI layer, and real-time updates should leverage the platform's WebSocket infrastructure (PLAT-105) when available.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Scoring service: scoreboard generation, ranking, score calculation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (CTF views: participant scoreboard, admin scoreboard, and scoreboard API endpoint)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFEvent model: scoreboard_visible, scoreboard_freeze_at, is_scoreboard_frozen)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Event service: scoreboard_visible in mutable fields whitelist)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/forms/` (CTFEventForm: scoreboard_visible field for organizer controls)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/migrations/0021_add_scoreboard_visible.py` (Migration: add scoreboard_visible field to CTFEvent)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/urls.py` (URL routing for scoreboard views and API endpoints)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_scoring.py` (Tests for scoreboard ranking, freeze logic, and visibility controls)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#576` (CTF-004: Scoreboard & Rankings)
