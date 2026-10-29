---
id: CTF-603
title: "Bulk Participant Import"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:22.828692Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-603: Bulk Participant Import

## Statement

The system should support bulk importing participants from a CSV file containing at minimum email addresses and display names. The import shall validate email format, detect duplicates within the file and against existing registrations, and report errors per row without failing the entire import. Successfully imported participants shall be auto-registered for the event.

## Rationale

Bulk import eliminates the need to individually invite dozens of participants. For Shifter enterprise events with 20-50 consultants, organizers typically have a roster spreadsheet. Manual one-by-one invitation is tedious and error-prone at scale.

## Traceability

- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#652` (CTF-603: Bulk Participant Import)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/bulk_import.py` (bulk_import_participants: CSV parsing, per-row validation, duplicate checks and partial success)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/api/organizer/participants.py` (ParticipantImportView organizer import endpoint)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/admin/ParticipantImportDialog.tsx` (Organizer CSV import dialog)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_participant_moderation_api.py` (test_bulk_import_partial_success)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_participant.py` (Bulk import deadline and capacity rules)
