---
id: CTF-1104
title: "CTFd Format Compatibility"
status: ACTIVE
type: INTERFACE
priority: COULD
wave: 2
created_at: 2026-03-18T05:28:24.069942Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-1104: CTFd Format Compatibility

## Statement

The system could support importing and exporting challenges in CTFd-compatible format (CTFd JSON export schema). Compatibility shall cover: challenge metadata, flags, hints, and file references. Shifter-specific fields (range integration, scheduling) that have no CTFd equivalent shall be omitted from CTFd-format exports and ignored during imports.

## Rationale

CTFd is the de facto standard CTF platform. Many publicly available challenge packs are distributed in CTFd format. Compatibility enables Shifter to leverage the existing CTF community's challenge libraries without requiring authors to reformat their content specifically for Shifter.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/transfer.py` (Native and CTFd-format challenge import and export)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/taxonomy.py` (CTFChallengeFile challenge file references)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/organizer/transfer.py` (Organizer import and export API views)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#632` (CTF-1104: CTFd Format Compatibility)
- DOCUMENTS → CODE_FILE `shifter/shifter_platform/ctf/views/admin_people.py` (CTF views - would need import/export endpoints)
