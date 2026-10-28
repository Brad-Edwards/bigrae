---
id: CTF-105
title: "Regex Flag Validation"
status: ACTIVE
type: FUNCTIONAL
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:21.549608Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-105: Regex Flag Validation

## Statement

The system could support regex-based flag validation where the organizer defines a regular expression pattern instead of a static string. Submitted flags shall be matched against the pattern, and a full match shall constitute a correct solve. The system shall validate that organizer-provided regex patterns are syntactically valid at challenge creation time.

## Rationale

Regex flags enable challenges where the answer has acceptable variations (for example different IP formats, variable whitespace, or dynamically generated per-participant flags). This is lower priority for Shifter since most range-based challenges use static flags, but it broadens the types of challenges organizers can create. (CTFd supports regex flags as a flag type.)

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (verify_single_flag(), regex validation via re.fullmatch(), add_flag(), pattern syntax validation)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTFFlag model, flag_type field with regex option, case_sensitive field)
