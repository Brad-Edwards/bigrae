---
id: CTF-1006
title: "Scheduler Auto-Start"
status: ACTIVE
type: FUNCTIONAL
priority: SHOULD
wave: 2
created_at: 2026-03-18T05:28:23.932719Z
updated_at: 2026-10-28T00:00:00Z
---

# CTF-1006: Scheduler Auto-Start

## Statement

The system should automatically start the CTF task scheduler when the application starts, without requiring manual intervention or separate process management. The scheduler shall detect and recover from crashes by re-evaluating all pending tasks on startup. The scheduler shall not duplicate task executions or miss scheduled tasks after application restart.

## Rationale

Manual scheduler startup is a deployment hazard, if someone deploys the application and forgets to start the scheduler, all automated tasks silently fail. Auto-start ensures the scheduler is always running when the application is running. Crash recovery prevents missed tasks after infrastructure incidents.

## Traceability

- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/apps.py` (CTF App Configuration)
- IMPLEMENTS → CONFIG `.github/workflows/_shifter-platform.yml` (CI/CD Workflow - deploys ctf-scheduler alongside portal)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/management/commands/run_ctf_scheduler.py` (CTF Scheduled Task Executor Management Command)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/ctf/models/` (CTF Models (CTFScheduledTask))
- IMPLEMENTS → CONFIG `shifter/shifter_platform/docker-compose.yml` (Docker Compose - ctf-scheduler service (restart: always))
- IMPLEMENTS → CONFIG `platform/k8s/gcp/base/ctf-scheduler-deployment.yaml` (GCP base ctf-scheduler Deployment with dedicated job-launcher token)
- IMPLEMENTS → CONFIG `platform/k8s/gcp/base/rbac-job-launcher.yaml` (GCP base job-launcher RBAC for ctf-scheduler)
- IMPLEMENTS → CONFIG `platform/k8s/gcp/base/serviceaccounts.yaml` (GCP base ctf-scheduler ServiceAccount)
- IMPLEMENTS → CONFIG `platform/charts/shifter/templates/ctf-scheduler-deployment.yaml` (Helm ctf-scheduler Deployment with dedicated job-launcher token)
- IMPLEMENTS → CONFIG `platform/charts/shifter/templates/rbac-job-launcher.yaml` (Helm job-launcher RBAC for ctf-scheduler)
- IMPLEMENTS → CONFIG `platform/charts/shifter/templates/serviceaccounts.yaml` (Helm ctf-scheduler ServiceAccount)
- IMPLEMENTS → CONFIG `platform/charts/shifter/values.yaml` (Helm ctfScheduler service account value)
- TESTS → TEST `shifter/shifter_platform/tests/platform/test_ctf_scheduler_startup.py` (CTF scheduler startup deployment invariants)
- TESTS → TEST `shifter/shifter_platform/tests/platform/test_gcp_job_launcher_manifests.py` (GCP job-launcher token and RBAC invariants)
- IMPLEMENTS → GITHUB_ISSUE `539` (Issue #539 - automated coverage and Ground Control TESTS trace links for active CTF requirements)
