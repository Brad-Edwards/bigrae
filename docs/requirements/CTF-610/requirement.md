---
id: CTF-610
title: "Participant Profile"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T07:23:12.088597Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-610: Participant Profile

## Statement

CTF events could provide an event-scoped participant profile view showing display name, affiliation, and solve history for the current event. Profile data shall be drawn from the platform's user profile (Management layer) plus CTF-specific event participation data. Participants could edit their display name and affiliation. The CTF layer shall not maintain a separate user profile store, it extends the platform profile with event-scoped views.

## Rationale

Participant profiles add a social dimension to CTF events and help organizers identify participants by affiliation. CTFd supports user profiles with team and solve information. For Shifter, affiliations are useful when running cross-team events where consultants from different regions participate.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFParticipant model (has name field, lacks affiliation field))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/participant_views.py` (ParticipantProfileView GET and PATCH for me/profile)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/profile.py` (update_own_profile)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/scoring/` (Scoreboard service (shows name/score but not full profile or solve history))
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#658` (CTF-610: Participant Profile)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/participant.py` (CTF views (no profile edit or public profile views exist))
