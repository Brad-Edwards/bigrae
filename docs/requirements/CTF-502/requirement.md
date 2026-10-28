---
id: CTF-502
title: "Team Creation"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:22.576603Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-502: Team Creation

## Statement

The system should allow registered participants to create teams when team mode is enabled. Team creation shall require a unique team name within the event. The participant who creates a team shall automatically become the team captain. Teams shall have a configurable maximum size enforced at join time.

## Rationale

Self-service team creation reduces organizer workload and lets participants form their own groups. For Shifter events, consultants from the same region or product team naturally want to group themselves without organizer intervention. (CTFd allows participant-driven team creation.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFTeam model - unique name constraint, captain FK, invite_code generation)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_models.py` (TestCTFTeamModel - test_create_team, test_team_unique_name_per_event)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#645` (CTF-502: Team Creation)
