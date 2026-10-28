---
id: CTF-013
title: "Administration"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:21.341312Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-013: Administration

## Statement

The CTF layer shall provide organizers with administrative dashboards and analytics delivered through Mission Control's UI layer, giving operational visibility into running events and historical performance data.

## Rationale

Organizers need operational visibility into running events, who is stuck, which challenges are too easy or too hard, whether ranges are healthy. Without administrative dashboards, organizers operate blind during events and cannot make real-time adjustments like releasing hints or extending time. CTF admin views are delivered within Mission Control, leveraging existing UI patterns and access controls.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Admin dashboard view - organizer overview with event stats)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Admin analytics view - per-event and per-challenge statistics)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Event detail view - real-time stats, status controls, management links)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Admin scoreboard view - live rankings with participant/team scores)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Admin range list view - range provisioning status per participant)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Event statistics service - participant count, submissions, points)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Challenge statistics service - solve count, solve rate, first blood)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/event/` (Event stats service - participants, challenges, submissions, points)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/_stats.py::get_challenge_statistics` (Challenge statistics service - event-roster solve-rate denominator, solve count, attempts, first blood)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_scoring_statistics.py` (Challenge statistics tests - roster denominator, attempts, solves, first blood, event scoping)
- IMPLEMENTS → GITHUB_ISSUE `539` (Issue #539 - automated coverage and Ground Control TESTS trace links for active CTF requirements)
