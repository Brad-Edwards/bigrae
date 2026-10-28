---
id: CTF-001
title: "Challenge Management"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:20.896712Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-001: Challenge Management

## Statement

The CTF layer shall provide challenge management capabilities including creating, organizing, configuring, and publishing CTF challenges with support for multiple flag types, categorization, difficulty levels, file attachments (using the platform's shared storage abstraction), prerequisites, visibility controls, and scheduling. Challenges are the content model that CTF adds to the Shifter platform.

## Rationale

Challenges are the core content unit of CTF events and the primary thing CTF adds to the Shifter platform. Organizers need a rich challenge management system to build engaging competitions with varied, well-structured challenge sets that make CTF events effective training exercises. (CTFd provides similar capabilities as a standalone platform; Shifter integrates these as a feature within its existing platform architecture.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF Models - Challenge, Event, Team, Participant, Submission models)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (CTF Challenge Service - CRUD, flag normalization, flag verification)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/forms/` (CTF Forms - CTFChallengeForm)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/enums.py` (CTF Enums - ChallengeCategory, ChallengeDifficulty, EventStatus)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_models.py` (CTF Model Tests)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#511` (CTF-001: Challenge Management)
