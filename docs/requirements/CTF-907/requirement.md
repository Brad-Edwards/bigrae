---
id: CTF-907
title: "MC + CTF Range Coexistence"
status: ACTIVE
type: CONSTRAINT
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.689350Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-907: MC + CTF Range Coexistence

## Statement

CTF range instances and Mission Control range instances shall coexist without conflict because they share the same underlying CMS/Engine provisioning pipeline. CTF participants are standard platform users whose ranges are provisioned via cms.services.create_range(), the same path Mission Control uses. Resource quota enforcement, if needed, should be implemented at the Engine or CMS layer to apply uniformly to all range consumers, not as CTF-specific logic.

## Rationale

CTF events and Mission Control demos share provisioning infrastructure. Concurrent events must not starve other range consumers. Engine and CMS capacity controls must account for shared bottlenecks as well as per-range limits. PLAT-201 and CTF-908 refine capacity planning and advance event declarations.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/bridges.py` (CTF-CMS bridge module (shared provisioning path))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF models (event config, max_participants))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/cms/services/` (CMS range creation service (shared infrastructure entry point))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/range/` (CTF range provisioning service (throttled provisioning))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/mission_control/views/` (Mission Control views (shared cms.services.create_range() call))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/auth.py` (Shared auth (CTF participants as standard platform users via group membership))
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_services/test_range.py` (CTF range provisioning tests (verifies shared CMS path))
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#546` (CTF-907: MC + CTF Range Coexistence)
