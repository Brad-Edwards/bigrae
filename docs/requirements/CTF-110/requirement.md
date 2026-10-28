---
id: CTF-110
title: "Challenge Visibility Control"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:21.739014Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-110: Challenge Visibility Control

## Statement

The system should support controlling per-challenge visibility with at least three states: visible (shown to all participants), hidden (not shown, accessible only to organizers), and locked (shown but not submittable, for example pending prerequisite completion or scheduled release). Organizers shall be able to change visibility at any time during an event.

## Rationale

Visibility control lets organizers stage challenges before making them available, hide broken challenges mid-event without deleting them, and gate access based on prerequisites or time. Without this, organizers have no way to prepare challenges in advance or recover from issues without deleting content. (CTFd supports similar visibility states.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFChallenge.visibility field, is_released checks visibility, is_visibility_locked property)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (get_available_challenges excludes hidden; visibility in mutable fields)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/submission.py` (submit_flag blocks hidden and locked challenges)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#544` (CTF-110: Challenge Visibility Control)
