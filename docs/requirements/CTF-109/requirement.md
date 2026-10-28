---
id: CTF-109
title: "Challenge Prerequisites"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:21.698126Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-109: Challenge Prerequisites

## Statement

The system could support defining prerequisite challenges that a participant must solve before a dependent challenge becomes visible or submittable. Prerequisites shall form a directed acyclic graph, circular dependencies shall be rejected at configuration time.

## Rationale

Prerequisites enable multi-stage challenge chains where each step builds on the previous (for example gain initial access, then escalate privileges, then exfiltrate data). For Shifter range-based challenges, prerequisites can mirror a realistic attack progression through a network. (CTFd supports challenge dependencies.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFChallengePrerequisite model, DAG with self-ref and same-event validation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (add_prerequisite, remove_prerequisite, check_prerequisites_met, get_available_challenges, BFS cycle detection)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_prerequisites.py` (Circular dependency detection, available challenges filtering, submit_flag blocking, cascade delete)
