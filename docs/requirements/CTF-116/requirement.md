---
id: CTF-116
title: "Flag Format Specification"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T07:23:07.670977Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-116: Flag Format Specification

## Statement

Each challenge could carry flag-format guidance (for example flag{...} or CTF{...}). The guidance depends on the environment pack: the pack supplies it with its CTF content, and organizers can edit it. The system shall display the guidance to participants with the challenge. Flag validation shall not enforce the format; it serves only as guidance to participants.

## Rationale

Flag format prefixes help participants distinguish flags from other strings they encounter during challenges. Displaying the expected format reduces support requests from participants who find a flag but are unsure if it is one. (CTFd supports configurable flag formats.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/challenge.py` (CTFChallenge.flag_format)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/content_bundle.py` (Pack CTF content supplies flag_format for each challenge)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/content_hydration.py` (Content hydration copies flag_format to the challenge)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/admin/ChallengeFormSections.tsx` (The organizer challenge form edits flag_format)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (verify_flag does not enforce flag_format prefix)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#2658` (Gap: participants do not see the flag-format guidance)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#538` (Closed: the scope depends on the environment pack)
