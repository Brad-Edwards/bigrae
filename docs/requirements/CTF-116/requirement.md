---
id: CTF-116
title: "Flag Format Specification"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T07:23:07.670977Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-116: Flag Format Specification

## Statement

The system could allow organizers to configure and display an expected flag format prefix per event (for example flag{...}, CTF{...}). The configured format shall be displayed to participants on the event page. Flag validation shall not enforce the format prefix, it serves only as guidance to participants.

## Rationale

Flag format prefixes help participants distinguish flags from other strings they encounter during challenges. Displaying the expected format reduces support requests from participants who find a flag but are unsure if it is one. (CTFd supports configurable flag formats.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFChallenge.flag_format field definition)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/forms/` (Challenge form includes flag_format for organizer configuration)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (verify_flag does not enforce flag_format prefix)
