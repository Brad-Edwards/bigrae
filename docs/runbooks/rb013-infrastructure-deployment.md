# RB013: Infrastructure deployment

Applicability: Terraform-owned GCP and optional AWS infrastructure. Operator:
platform owner. Verified: 2026-10-23.

## Symptom

Infrastructure drift or a reviewed change requires deployment, or Terraform
plan/apply fails for the selected environment.

## Investigation

1. Confirm the selected backend/environment from validated `shifter.yaml`, the
   correct Terraform root and remote state, the approved Git revision, and the
   protected cloud identity.
2. Review the saved plan by resource address and action. Separate expected
   release changes from drift, replacements, IAM/network expansion, and
   destructive actions.
3. Check remote-state lock ownership, provider quota, private networking,
   deletion protection, policy checks, and any concurrent deploy/destroy run.
4. Resolve names from Terraform outputs. Never dump all outputs: some are
   sensitive, and state is not an incident attachment.

## Resolution

Use the protected `Deploy` workflow and its saved, reviewed plan. Repair code or
configuration at the Terraform owner and rerun validation; do not console-edit
resources or apply from an operator laptop. If a lock is active, identify the
owning run and wait or escalate—do not force-unlock while mutation may still be
running. Verify Terraform readback, cluster/workload rollout, provider health,
and application smoke.

## Prevention

Serialize deployments per environment, keep state/versioning and workload
identity protected, review destructive plans, monitor drift and quota, and
retain plan/apply evidence. Use the provider-specific destroy workflow only for
an explicitly approved full teardown.

Escalate replacement of stateful/network/security resources, unexplained drift,
stale lock ambiguity, missing protected identity, or any manual workaround that
would leave Terraform unaware.
