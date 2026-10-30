---
id: CTF-503
title: "Team Captains"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:22.611975Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-503: Team Captains

## Statement

The system should designate one member per team as captain. The captain shall have the ability to: rename the team, remove members, transfer captaincy to another member, and dissolve the team (if no solves have been recorded). Organizers shall be able to reassign captaincy.

## Rationale

Teams need a designated manager for administrative actions. Without a captain role, every team change requires organizer intervention, which does not scale for events with many teams. (CTFd supports team captains with management privileges.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/team.py` (CTFTeam.captain)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/team.py` (Captain-only rename, remove member, transfer captaincy and disband)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/team_views.py` (Participant team API for the captain actions)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/admin/_participant.py` (Django admin CTFTeamAdmin can edit the captain field)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_team_api.py` (Creator becomes captain; transfer, captain-only guard and disband)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#646` (Gap: disband is allowed after solves, and organizers cannot reassign captaincy in the app)
