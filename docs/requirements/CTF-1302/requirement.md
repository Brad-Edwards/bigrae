---
id: CTF-1302
title: "Analytics Dashboard"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:24.239482Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-1302: Analytics Dashboard

## Statement

The system should provide an analytics dashboard with event performance insights including: score distribution histogram, solve timeline (solves over time), challenge difficulty analysis (solve rate vs. point value), participant engagement metrics (active time, challenges attempted), and comparison across events. Analytics shall be available during and after events.

## Rationale

Analytics transform raw event data into actionable insights for improving future events. Understanding which challenges were too easy, which were never solved, and when participant engagement dropped informs better challenge design and event scheduling. CTFd provides basic statistics; deeper analytics differentiate the Shifter platform.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/` (Analytics view - shows participant count, challenge count, submissions, per-challenge stats)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Scoring service - get_event_analytics score histogram, solve timeline, challenge analysis and engagement)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/urls.py (admin_analytics route)` (URL route for analytics dashboard at admin/events/<uuid>/analytics/)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#636` (CTF-1302: Analytics Dashboard)
