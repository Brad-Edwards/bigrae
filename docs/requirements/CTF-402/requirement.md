---
id: CTF-402
title: "Team Scoreboard"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:22.323485Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-402: Team Scoreboard

## Statement

The system should display a team scoreboard when team mode is active, ranking teams by aggregate team score. Team score shall be the sum of all team members' individual scores. The scoreboard shall show: team rank, team name, total score, number of unique challenges solved by any team member, and team member count.

## Rationale

Team scoreboards aggregate individual efforts into a team competition. For Shifter, team mode enables consultant groups to collaborate on range-based challenges while competing against other teams. (CTFd supports team scoreboards that roll up member scores.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (get_team_scoreboard() - team rankings with aggregate score, solve_count, member_count)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_scoring.py` (Team scoreboard scoring tests including unique challenge solve count)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#573` (CTF-402: Team Scoreboard)
