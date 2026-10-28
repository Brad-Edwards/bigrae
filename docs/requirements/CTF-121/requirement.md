---
id: CTF-121
title: "Next Challenge Navigation"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T20:39:01.445456Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-121: Next Challenge Navigation

## Statement

The system should support configuring a "next challenge" link per challenge that guides participants to a suggested follow-up challenge after solving. This is distinct from prerequisites (which gate access), next-challenge navigation is a non-blocking UX recommendation. When a participant solves a challenge with a configured next challenge, the UI shall offer navigation to it.

## Rationale

For training-focused events, guided progression is valuable, organizers can design learning sequences where challenges build on each other conceptually without hard-gating access. A non-blocking "next challenge" recommendation helps participants follow a structured learning path after each solve. (CTFd supports a similar Next Challenge configuration.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/challenge.py` (CTFChallenge.next_challenge)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/forms/` (Challenge form - next_challenge field with event-scoped queryset)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_models.py` (TestCTFChallengeModel - next_challenge validation tests)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#568` (CTF-121: Next Challenge Navigation)
