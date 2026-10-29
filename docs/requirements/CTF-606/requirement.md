---
id: CTF-606
title: "Hidden Users"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:22.945170Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-606: Hidden Users

## Statement

The system could support hiding specific participants from the public scoreboard while allowing them to continue participating normally. Hidden participants shall still be able to view their own scores and submit flags. Their scores shall not affect other participants' rankings. Organizers shall see hidden participants in the admin view.

## Rationale

Hidden users enable organizers or test accounts to participate without distorting the competitive scoreboard. For Shifter, organizers may want to test challenges during a live event without appearing on the leaderboard and confusing real participants. (CTFd supports hidden users.)

## Traceability

- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#655` (CTF-606: Hidden Users)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/team.py` (CTFParticipant.hidden field)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/organizer/moderation.py` (ParticipantHiddenView sets the hidden flag)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/queries.py` (The ranked-participant predicate excludes hidden participants; they keep their own score)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_participant_moderation_api.py` (test_hidden_leaves_play_but_drops_rankings)
