---
id: CTF-904
title: "Range Connection Info"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 2
created_at: 2026-03-18T05:28:23.574150Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-904: Range Connection Info

## Statement

The CTF participant range page shall surface range connection information from the existing Engine and CMS services: VM hostnames or IP addresses, OS types, and provisioning status. Connection info shall only be visible to the participant who owns the range. Browser-based access links shall point to the Mission Control terminal page. Credentials are managed by the Engine provisioning pipeline and displayed via the existing platform UI. The CTF layer shall not duplicate connection info assembly.

## Rationale

Participants need to know how to connect to their range VMs and what credentials to use. Some challenges require direct IP access rather than Guacamole. Connection information must be per-participant and private to prevent participants from accessing each other's environments.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/` (Range service: assembles Guacamole URL with credentials for participant access)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_range.py` (Range service tests - provisioning, status, cleanup, destroy)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#569` (CTF-904: Range Connection Info)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/provision.py` (Range service - provisioning (delegates to CMS/Engine))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/status.py` (Range service - status reads (delegates to CMS/Engine))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/lifecycle.py` (Range service - lifecycle stop/start/restart/destroy + cleanup (delegates to CMS/Engine))
