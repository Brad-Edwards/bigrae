---
id: PLAT-002
title: "GCP Platform Deployment"
status: ACTIVE
type: FUNCTIONAL
priority: MUST
wave: 1
created_at: 2026-03-19T02:57:48.822709Z
updated_at: 2026-05-09T05:11:30.392223Z
---

# PLAT-002: GCP Platform Deployment

## Statement

The platform shall support deployment of all platform services on Google Cloud Platform using GCP-native equivalents for each service dependency. This includes but is not limited to: application hosting (Cloud Run or GKE), relational database (Cloud SQL for PostgreSQL), in-memory cache (Memorystore for Redis), object storage (Cloud Storage), asynchronous task processing, and secret management (Secret Manager). The GCP deployment shall achieve functional parity with the AWS deployment.

## Rationale

Supporting GCP as a first-class deployment target enables Shifter to run in organizations or environments where AWS is not available or not preferred. Functional parity ensures that users on GCP deployments have the same capabilities as those on AWS, preventing a two-tier experience.

## Traceability

- IMPLEMENTS → CONFIG `shifter/shifter_platform/config/settings.py` (Django settings - hardcoded AWS service dependencies (SES us-east-2, SQS queues, ECS clusters))
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/shared/management/commands/run_worker.py` (SQS worker - polls AWS SQS directly via boto3, no message queue abstraction)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/engine/ecs.py` (ECS task launcher - uses AWS ECS Fargate for provisioner execution)
- IMPLEMENTS → CONFIG `platform/terraform/` (Terraform modules - 100% AWS provider (ECS, RDS, Cognito, SES, SNS, SQS, S3), no GCP modules)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/shifter#671` (PLAT-002: GCP Platform Deployment)
- DOCUMENTS → ADR `ADR-007` (GCP control-plane deployments are Helm-packaged and bootstrap-managed)
- DOCUMENTS → ADR `ADR-008` (GCP bootstrap fails closed and uses private operator access)
- DOCUMENTS → POLICY `docs/adr/index.yaml` (ADR registry - GCP Helm cutover and secure bootstrap decisions)
- DOCUMENTS → DOCUMENTATION `platform/terraform/gcp/README.md` (GCP Terraform README - control-plane scope and hardened posture)
- DOCUMENTS → DOCUMENTATION `docs/architecture/github-less-gcp-deployment-design-2571.md` (GitHub-less GCP deployment design - release, deploy, and teardown data flows)
- DOCUMENTS → DOCUMENTATION `docs/runbooks/rb023-gcp-deployment.md` (RB023 - GitHub-less GCP deploy, update, or teardown failed)
- DOCUMENTS → DOCUMENTATION `docs/technical/handbook/gcp-github-less-deployment.md` (GitHub-less GCP deployment guide)
- DOCUMENTS → DOCUMENTATION `shifter/shifter_platform/documentation/docs/technical/platform_infrastructure/gcp-infrastructure.md` (GCP infrastructure technical architecture)
- DOCUMENTS → DOCUMENTATION `shifter/shifter_platform/documentation/docs/technical/dev/ci-cd.md` (Developer CI/CD docs - authoritative GCP bootstrap path)
- DOCUMENTS → ADR `ADR-009` (AWS and GCP keep provider-specific identity stacks behind a shared auth seam)
- DOCUMENTS → DOCUMENTATION `shifter/shifter_platform/documentation/docs/technical/architecture.md` (Shifter Architecture)
- IMPLEMENTS → CONFIG `platform/charts/shifter/Chart.yaml` (GCP Shifter Helm chart)
- IMPLEMENTS → CONFIG `platform/charts/shifter/values-gcp-dev.yaml` (GCP dev Helm values)
- IMPLEMENTS → CONFIG `platform/terraform/gcp/modules/platform-core/main.tf` (GCP platform core Terraform module)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/deploy.py` (GCP bootstrap deployment entrypoint)
- IMPLEMENTS → CODE_FILE `scripts/gcp/render_runtime_env.py` (GCP runtime environment renderer)
- TESTS → TEST `scripts/gcp/tests/test_render_runtime_env.py` (GCP runtime env renderer tests)
- CONSTRAINS → ADR `ADR-005` (Cloud expansion must preserve provider seams and AWS continuity)
- CONSTRAINS → ADR `ADR-006` (Kubernetes workloads must meet Pod Security Standards)
- CONSTRAINS → ADR `ADR-007` (GCP control-plane deployments are Helm-packaged and bootstrap-managed)
- CONSTRAINS → ADR `ADR-008` (GCP bootstrap fails closed and uses private operator access)
- CONSTRAINS → ADR `ADR-009` (AWS and GCP keep provider-specific identity stacks behind a shared auth seam)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/config/_email.py` (GCP transactional email backend selection (anymail SendGrid/Mailgun), PLAT-002 parity)
- IMPLEMENTS → CODE_FILE `shifter/shifter_platform/config/capacity_metrics_gcp.py` (GCP Cloud Monitoring publisher for portal capacity metrics, PLAT-002 parity)
- TESTS → TEST `shifter/shifter_platform/tests/config/test_email.py` (Tests for GCP email backend selection + never-committed ESP key handling)
- TESTS → TEST `shifter/shifter_platform/tests/config/test_capacity_metrics_gcp.py` (Tests for GCP Cloud Monitoring capacity-metrics adapter (translation, write path, fail-soft, provider routing))
- TESTS → TEST `scripts/bootstrap/tests/test_gcp_control_plane.py` (GCP control-plane bootstrap tests (split from test_deploy.py, #687))
- TESTS → TEST `scripts/bootstrap/tests/test_gdc_cluster.py` (GDC cluster bootstrap tests (split from test_deploy.py, #687))
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_control_plane.py` (GCP control-plane Helm values, runtime env, operator identity, namespaces, migrations, and the GDC substrate, split from deploy.py, #687)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gdc_cluster.py` (GDC VM Runtime cluster bootstrap (network + substrate setup), split from deploy.py, #687)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_cli.py` (GCP lifecycle commands gcp-deploy, gcp-release-scan, gcp-smoke, gcp-destroy, and gcp-config, #2571)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_deploy.py` (GCP deploy from a published release: inputs, identity, lock, and release gates, #2571)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_deploy_stages.py` (GCP deploy infrastructure and rollout stages, #2571)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_terraform.py` (Platform Terraform run with private var files for the GCP deploy, #2571)
- IMPLEMENTS → CODE_FILE `scripts/bootstrap/gcp_destroy.py` (GCP teardown with the destroy identity, #2571)
- IMPLEMENTS → CONFIG `.github/workflows/release-gcp.yml` (GCP release workflow - control-plane images, deployer image, and provenance, #2571)
- IMPLEMENTS → CONFIG `platform/deployer/gcp/Dockerfile` (GCP deployer image, #2571)
- TESTS → TEST `scripts/bootstrap/tests/test_gcp_deploy_commands.py` (GCP lifecycle command tests, #2571)
- TESTS → TEST `scripts/bootstrap/tests/test_gcp_deploy_stages.py` (GCP deploy stage tests, #2571)
- TESTS → TEST `scripts/bootstrap/tests/test_gcp_release_gates.py` (ADR-037 release gate tests for the GCP deploy, #2571)
- DOCUMENTS → ADR `ADR-070` (GCP deployments run from published releases without GitHub)
- CONSTRAINS → ADR `ADR-070` (GCP deployments run from published releases without GitHub)
- DOCUMENTS → GITHUB_ISSUE `Brad-Edwards/bigrae#2571` (GitHub-less GCP deployment)
