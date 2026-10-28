---
id: CTF-102
title: "Challenge Categories"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:21.446320Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-102: Challenge Categories

## Statement

The system shall support organizing challenges into named categories (for example web exploitation, cryptography, forensics, reverse engineering, pwn, OSINT, misc). Categories shall be definable per event by organizers. The challenge listing UI shall group and filter challenges by category.

## Rationale

Categories are the primary organizational axis for CTF challenges. Participants use categories to find challenges matching their skill set, and without them, a flat list of 20+ challenges becomes unnavigable, participants waste time finding relevant challenges instead of solving them. (CTFd uses categories as its core organizational structure.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (ChallengeCategory enum - defines 9 named categories (web, forensics, crypto, reverse, pwn, misc, osint, hardware, network))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFChallenge model - category CharField with ChallengeCategory.choices(), indexed, ordered by category)
