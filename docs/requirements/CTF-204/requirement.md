---
id: CTF-204
title: "Awards/Bonus Points"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:22.000543Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-204: Awards/Bonus Points

## Statement

The system could support organizer-granted bonus point awards to participants outside of challenge solves. Awards shall have a name, description, point value (positive or negative), and recipient. Awards shall appear in the score breakdown and contribute to the participant's total score.

## Rationale

Awards enable organizers to recognize exceptional behavior (creative solutions, helping others, finding unintended bugs) or penalize rule violations without disqualifying. They provide flexibility for situations the scoring system cannot anticipate, such as ad-hoc bonuses or deductions during live events. (CTFd supports awards as a similar scoring mechanism.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/submission.py` (CTFAward award and bonus points model)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#642` (CTF-204: Awards/Bonus Points)
