---
id: PLAT-206
title: "NGFW Lifecycle and Access Management"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-05-09T05:11:30.245289Z
updated_at: 2026-10-28T00:00:00Z
---

# PLAT-206: NGFW Lifecycle and Access Management

## Statement

The platform shall support NGFW-backed range and demo workflows by modeling firewall credentials and deployment profiles, provisioning and destroying persistent user NGFW instances where configured, exposing management and connection information through Mission Control, and carrying provider-specific firewall attachment metadata without leaking it into unrelated domain models.

## Rationale

NGFW provisioning, views, Terraform support, and operator tooling exist but were not covered by an explicit shifter requirement.

## Traceability

- IMPLEMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#244` (NGFW Database Models & Migrations)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#613` (Implement GCP VM-Series / NGFW support)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#426` (Add NGFW web GUI access button to detail page)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/mission_control/views/` (Mission Control NGFW views and APIs)
- IMPLEMENTS → CONFIG `shifter/shifter_platform/mission_control/urls.py` (NGFW route registration)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/config/` (Provider-neutral NGFW attachment resolution)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/main.py` (NGFW provisioning and teardown paths)
- IMPLEMENTS → CONFIG `platform/terraform/modules/range/vpc/ngfw.tf` (Persistent NGFW range infrastructure)
- DOCUMENTS → DOCUMENTATION `docs/features/ngfw.md` (NGFW user documentation)
- TESTS → TEST `shifter/shifter_platform/tests/engine/ecs/test_start_ngfw_provisioning.py` (NGFW provisioning task tests)
