---
id: CTF-115
title: "Per-Challenge Connection Info"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T07:23:03.277450Z
updated_at: 2026-10-29T00:00:00Z
---

# CTF-115: Per-Challenge Connection Info

## Statement

The system should support associating specific connection information (for example host, port, service endpoint) with individual challenges. When a challenge references a specific service on a participant's range VM, the system shall display the participant-specific connection details alongside the challenge description.

## Rationale

Range-integrated challenges often target specific services on specific VMs. Participants need to know which host:port to attack for each challenge. Without per-challenge connection info, participants must manually figure out which VM and port correspond to each challenge, adding unnecessary friction.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/challenge.py` (CTFChallenge.target_instance_name and target_port)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/participant/play.py` (resolve_target_connection_info resolves the participant's own target connection details)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/bridges.py` (cms_get_range_target_instances bridge to CMS)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/services/challenge/` (_CHALLENGE_MUTABLE_FIELDS includes target_instance_name and target_port)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/frontend/src/features/ctf/ChallengeDetailPage.tsx` (The participant challenge page shows connection_info)
- TESTS → TEST `shifter/shifter_platform/tests/ctf/test_range_target_instances.py` (Participant challenge connection_info uses the exact assigned range targets)
- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#548` (CTF-115: Per-Challenge Connection Info)
