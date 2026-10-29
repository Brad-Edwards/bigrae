---
id: PLAT-003
title: "GCP Range Provisioning"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 1
created_at: 2026-03-19T02:57:48.879958Z
updated_at: 2026-10-28T00:00:00Z
---

# PLAT-003: GCP Range Provisioning

## Statement

The range provisioning engine shall support creating, managing, and destroying range instances on GCP. This includes: provisioning compute instances (Compute Engine), configuring networking (VPC, firewall rules, Cloud NAT), providing remote access to range VMs (for example, via Apache Guacamole or equivalent), and managing range lifecycle (start, stop, restart, destroy). GCP range provisioning shall support the same scenario templates and range configurations as the AWS provisioning path.

## Rationale

Ranges are the core infrastructure product of Shifter. If the platform deploys on GCP, ranges must also run on GCP, participants cannot access AWS VMs from a GCP-hosted platform without cross-cloud networking complexity. Same-cloud range provisioning keeps the architecture simple and latency low.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/components/instance.py` (Instance component - creates AWS EC2 instances with AMIs, no GCP Compute Engine support)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/components/network/` (Network component - creates AWS VPC subnets/security groups/route tables)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/executors/ssm_executor.py` (SSM Executor - uses AWS SSM for remote command execution on range VMs)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/config/` (Provisioner config - RangeConfig uses AWS-specific fields (AMI IDs, VPC ID, instance profiles))
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#672` (PLAT-003: GCP Range Provisioning)
- DOCUMENTS → DOCUMENTATION `docs/technical/platform_infrastructure/networking.md` (Networking architecture - GCP platform and range connectivity)
- DOCUMENTS → DOCUMENTATION `docs/technical/platform_infrastructure/gdc-provisioning.md` (GDC provisioning architecture - runtime primitives and range networking)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/range_terraform_runner.py` (Provider-routed range Terraform runner)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/range_ops/` (Provider-routed range lifecycle operations)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/main.py` (GCP-aware range provisioning state persistence)
- TESTS → TEST `shifter/shifter_platform/tests/engine/ecs/test_start_ecs_task_gcp.py` (GCP task runner tests)
- CONSTRAINS → ADR `ADR-005` (Cloud expansion must preserve provider seams and AWS continuity)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/raes_gcp_apply.py` (Native GCE range realization through the shared plan and resource primitives)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/gcp_range_cell_ops.py` (Bounded Compute Engine operation waits and resource lifecycle helpers)
- IMPLEMENTS → CODE_FILE `shifter/engine/provisioner/range_ops/__init__.py` (Provider-routed range lifecycle operations)
- IMPLEMENTS → CONFIG `platform/terraform/gcp/environments/platform/main.tf` (Shared GCP platform and range prerequisites, configured per deployment)
