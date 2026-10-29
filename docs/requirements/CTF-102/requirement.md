---
id: CTF-102
title: "Challenge Categories"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:21.446320Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-102: Challenge Categories

## Statement

The system shall support organizing challenges into named categories (for example web exploitation, cryptography, forensics, reverse engineering, pwn, OSINT, misc). Categories shall be definable per event by organizers. The challenge listing UI shall group and filter challenges by category.

## Rationale

Categories are the primary organizational axis for CTF challenges. Participants use categories to find challenges matching their skill set, and without them, a flat list of 20+ challenges becomes unnavigable, participants waste time finding relevant challenges instead of solving them. (CTFd uses categories as its core organizational structure.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/challenge.py` (CTFChallenge.category: organizer-authored free text for each challenge, indexed and used for ordering)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/ChallengesPage.tsx` (The participant challenge list groups challenges by category)
- TESTS → TEST `shifter/shifter_platform/frontend/src/features/ctf/ChallengesPage.test.tsx` (Category grouping in the participant challenge list)
