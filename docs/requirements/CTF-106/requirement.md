---
id: CTF-106
title: "Case-Insensitive Flags"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:21.583957Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-106: Case-Insensitive Flags

## Statement

The system could support a per-flag option to enable case-insensitive matching. When enabled, flag comparison shall normalize both the stored flag and submitted value to the same case before comparison. This option shall be configurable independently per flag.

## Rationale

Case sensitivity is a common source of frustration when participants find the correct answer but submit it in the wrong case (for example FLAG{answer} vs flag{answer}). For training-focused events where the goal is learning rather than strict competition, case insensitivity reduces unnecessary friction. (CTFd supports a case-insensitive flag option.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFFlag.case_sensitive boolean field, configurable per flag)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (verify_single_flag(), lowercase normalization for static, `re.IGNORECASE` for regex)
