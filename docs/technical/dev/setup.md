# Setup

Deploy BigRAE from a cloud account to a running environment.

## Before You Start

Work through this checklist before running any command below. A fresh tenant
standup fails partway if an item is missing, so confirm all of them first.

### Local tooling

- Python 3.12+
- `uv`
- Terraform 1.7+
- GitHub CLI (`gh`) authenticated
- Docker
- **AWS**: AWS CLI v2 configured with SSO or IAM credentials
- **GCP**: `gcloud` CLI authenticated with appropriate project. A GCP deployment
  without GitHub runs the commands from the release's deployer image, which holds
  the other tools. Docker, `oras`, and `cosign` get and verify that image. See
  the [GitHub-less GCP deployment guide](../handbook/gcp-github-less-deployment.md).

### Accounts and access

- A cloud account (AWS or GCP) with billing enabled and permissions to create
  IAM, networking, compute, database, and storage resources.
- Admin access to the GitHub repository to set Actions secrets and variables
  (Settings, then Secrets and variables, then Actions). A GCP deployment without
  GitHub does not need it.
- A domain you control, plus access to its DNS provider. It is required for
  certificate validation (ACM on AWS, managed TLS on GCP) and to point the
  hostname at the load balancer.

### Must already exist before the first Terraform apply (AWS)

These are hard preconditions; the Portal stack plan fails without them. See
[`aws-terraform-apply-order.md`](../../dev/aws-terraform-apply-order.md)
for the authoritative order.

- **Bootstrap has run** for the environment (`scripts/bootstrap/deploy.py
  bootstrap`), creating the S3 state bucket, GitHub OIDC provider, and deploy
  IAM role. This doc walks through it under [AWS Deployment](#aws-deployment).
- **Self-hosted runners provisioned and registered**, if deploying through CI
  (all AWS deploy jobs use `runs-on: self-hosted`). See
  [`aws-runner-provisioning-runbook.md`](../../dev/aws-runner-provisioning-runbook.md).
- **Range AMIs built and the `/shifter/ami/{kali,ubuntu,windows,dc}` SSM
  parameters seeded.** The Portal stack reads these as data sources. See
  [`aws-ami-seeding-runbook.md`](../../dev/aws-ami-seeding-runbook.md).
- **Engine provisioner image built** (CI `_shifter-engine.yml`) so the Portal
  stack can resolve its image digest.

### Must already exist before deploy (GCP)

- A GCP project with billing enabled, and every Vertex AI model that the
  configuration uses enabled.
- The foundation, applied with `deploy.py gcp-foundation`:
  - without GitHub: the purpose identities with named impersonation
    principals, and the deployment config bucket;
  - for CI: Workload Identity Federation for GitHub Actions (pool, provider,
    purpose service accounts), with the explicit `GCP_*_SERVICE_ACCOUNT` value
    and `GCP_WORKLOAD_IDENTITY_PROVIDER` set in each purpose Environment.
- Without GitHub: a published release that the deployment can read, the
  deployment input bundle in the config bucket, and the first operator password
  in Secret Manager.
- A release that selects the Kali, Ubuntu, and DC guest images. `deploy.py
  gcp-images` verifies and imports them as native GCE images; no
  platform-owned transfer bucket must already exist. See
  [`gcp-range-cell-deploy.md`](../../dev/gcp-range-cell-deploy.md) and
  [`gcp-guest-images.md`](../../architecture/gcp-guest-images.md).

### Configuration values

- The single authoritative checklist of every secret and repository variable a
  fresh environment needs, and how each is populated, is in
  [`deploy-secrets.md`](../../dev/deploy-secrets.md). The deploy
  preflight enforces these.

## Root Installation Config

Scaffold, edit, then validate `bigrae.yaml` before deployment. `init` copies the
checked example for your backend to `./bigrae.yaml` (local-only: it writes no
secrets and calls no cloud API):

```bash
# Scaffold ./bigrae.yaml (run `init` with no --backend to list the available backends).
uv run --project shifter/installation bigrae-config init --backend aws

# Edit bigrae.yaml for your deployment, then validate its shape.
uv run --project shifter/installation bigrae-config validate bigrae.yaml
```

Use `--backend gcp` for GCP. See [Installation Config](installation-config.md) for the
field reference.

## Doctor

Once `bigrae.yaml` is filled in, run `doctor` to validate the *selected backend* (not
just the config shape) before applying infrastructure. It runs the checks the backend
bundle declares (required tools on PATH, secret references, generated outputs, owned
repository paths, and the bundle's credential-free validation checks) and labels each
check as local-only, cloud-read-only, or deployment-mutating. It is non-mutating by
default and its output is backend/profile based, so you never need to read the deploy
workflow's branch logic to understand a failure.

```bash
# Local-only checks (default): config, tools, secret references, non-mutating validation.
uv run --project shifter/installation bigrae-config doctor bigrae.yaml

# Additionally run read-only health probes of the deployment endpoint (post-deploy).
uv run --project shifter/installation bigrae-config doctor bigrae.yaml --checks cloud
```

`doctor` exits `1` when any blocking check fails. It is a pre-mutation readiness signal;
the deploy preflight below, Terraform, and runtime health gates remain authoritative for
their own layers.

## Preflight

The deploy preflight is the shared, fail-safe check that every prerequisite is in
place before any change is made. It runs the same checks locally and in CI, so a
missing secret, tool, or config fails up front with a consolidated report instead
of partway through a Terraform apply.

Run it on demand before deploying:

```bash
# Validate an AWS environment (add --component core|range|portal to scope overlays)
./scripts/bootstrap/deploy.py preflight --cloud aws --env dev

# Validate a GCP deployment input bundle (or pass --inputs <dir>)
./scripts/bootstrap/deploy.py preflight --cloud gcp --env <environment> --store gs://<deployment-config-bucket>
```

The `bootstrap`, `terraform`, and `full` commands run the preflight automatically
at the start. It is interactive by default: it prints the results and asks you to
confirm the manual prerequisites it cannot verify (cloud authentication, DNS
access). Pass `--headless` (or run without a TTY, as CI does) to skip the prompts
and fail on any missing required prerequisite instead.

In CI, the first Identity Platform operator credentials
(`GCP_BOOTSTRAP_ADMIN_EMAIL` / `GCP_BOOTSTRAP_ADMIN_PASSWORD`) are required. To
deliberately skip operator creation, set the `SHIFTER_SKIP_OPERATOR_BOOTSTRAP`
repository variable to `true`; the skip is logged, never silent. A GCP
deployment without GitHub declares the operator in
`settings.operator_bootstrap`; set `enabled: false` to skip it.

## Environments and Terraform Layout

GCP uses one execution root, `platform/terraform/gcp/environments/platform/`.
The four environment directories contain values only.
AWS retains separate `dev` and `prod` Core, Range, Portal and EKS roots.
The AWS production roots are owned by @Brad-Edwards and remain available through
the bootstrap `terraform` and `full` commands and the EKS lifecycle.

### Terraform directories

```text
platform/terraform/
  environments/<env>/             AWS Core stack
    range/                         AWS Range stack
    portal/                        AWS services and data plane
    eks/                           AWS Kubernetes control plane
  gcp/environments/platform/      Shared GCP execution root
  gcp/environments/<env>/         GCP values and private local overrides
  global/                         AWS account-wide infrastructure
  modules/                        Reusable AWS modules
  gcp/global/                     Independent GCP foundation and runner roots
  gcp/modules/                    Reusable GCP modules
```

### Deployment selection

Deployments use an explicit `workflow_dispatch` environment choice.
Pushes and pull requests run validation only.
The maintained choices are `aws-dev`, `gcp-dev`, `orthanc`, `sauron` and `balrog`.
Branch names do not select an environment.
AWS production remains a local operator path; it is not a deploy workflow choice.
See [CI/CD](../platform_infrastructure/cicd.md) for the trigger matrix.

### Adding a GCP environment

A GCP deployment without GitHub needs no change to this repository. Apply its
foundation, publish its deployment input bundle, and deploy a release
([GitHub-less GCP deployment guide](../handbook/gcp-github-less-deployment.md)).

A GCP CI deployment needs these repository changes:

1. Add its value file under `gcp/environments/<environment>/`.
2. Configure its protected GitHub Environments and purpose identities.
3. Add its explicit workflow authorization choice.
4. Use a separate state bucket/prefix.

Do not copy the shared Terraform source.
See [deployment configuration and recovery](../../dev/deployment-configuration.md).

## AWS Deployment

AWS platform workloads run on EKS through the shared Helm chart.
Prepare Core, Range and Portal infrastructure using the
[AWS apply order](../../dev/aws-terraform-apply-order.md).
The Portal root owns persistent AWS services; it does not deploy the application containers.

The maintained application entrypoint uses a validated installation config and attested image digests:

```bash
./scripts/bootstrap/deploy.py eks-deploy \
  --config shifter.yaml --images .shifter/images.json --profile <aws-profile>
```

Follow the [AWS EKS bundle](../platform_infrastructure/aws-eks-bundle.md)
and [EKS operations](../../ops/portal-eks-operations.md) for prerequisites, protected inputs,
workload identities, ingress, deployment checks and teardown.
Terraform owns cloud infrastructure. Helm owns namespaced platform workloads.
Legacy EC2 portal deployment scripts are not supported entrypoints.

The retained infrastructure-only CLI remains available for `dev` and `prod`:

```bash
./scripts/bootstrap/deploy.py bootstrap --env dev --profile <aws-profile>
./scripts/bootstrap/deploy.py terraform --env dev --profile <aws-profile>
```

Bootstrap renders private backend files in the operator instance directory.
Pass those files to Terraform init; do not edit committed backend examples.
Configure the exact deployment values described in [deploy secrets](../../dev/deploy-secrets.md).

## Existing Environment Setup

If deploying to an account with existing infrastructure:

### Initialize Terraform

```bash
cd platform/terraform/environments/prod/portal
AWS_PROFILE=<your-profile> terraform init
```

### Verify State

```bash
AWS_PROFILE=<your-profile> terraform plan
```

No changes means infrastructure matches state.

## Common Issues

### State Lock Error

Someone else is running Terraform, or a previous run crashed:

```bash
# Check who has the lock, then force unlock if needed
terraform force-unlock <LOCK_ID>
```

### Provider Cache Issues

```bash
rm -rf .terraform
terraform init
```

### ACM Validation Timeout

If Terraform times out waiting for ACM:

```bash
# 1. Get the validation records again
terraform output -json acm_validation_records

# 2. Check if DNS is propagated (replace with your actual record name)
dig CNAME _abc123.shifter.yourdomain.com

# 3. If DNS is correct, just re-run apply (it will resume)
terraform apply
```

Common issues:
- **Trailing dot**: Some DNS providers need the trailing `.` removed from the value
- **Wrong record type**: Must be CNAME, not TXT
- **Propagation delay**: Wait 5-10 minutes after adding records

### Container Pull Failures

Check ECR repo exists and has images:

```bash
aws ecr describe-images --repository-name shifter-prod-portal
```

If empty, push a container first.

## GCP Deployment

GCP provisions with Terraform (`platform/terraform/gcp/`, rooted at
`environments/platform`, whose main module is `modules/platform-core/`). The
Helm chart `platform/charts/shifter/` is the only Kubernetes renderer. The
`deploy.py gcp-deploy` command runs the first install and every update
(ADR-070).

There are two ways to run it. Both run the same command:

| Path | Inputs | Runner |
| --- | --- | --- |
| Without GitHub | The deployment input bundle in the deployment config bucket, and Secret Manager | An operator runs the release's deployer image ([guide](../handbook/gcp-github-less-deployment.md)). |
| CI | GitHub secrets and variables ([deploy-secrets.md](../../dev/deploy-secrets.md#gcp-gcp-dev)) | `deploy.yml` calls `_gcp-dev.yml`, which renders the same bundle. |

### 1. GCP Project Setup

Create a GCP project. Before starting bootstrap, enable every Google Cloud API
required by the selected BigRAE configuration. If the configuration uses AI
models, also enable every configured model in Vertex AI Model Garden: an
authorized project administrator must open each model, accept any provider or
Marketplace terms, and confirm that it is available in the configured region.
Enabling the Vertex AI API does not enable individual models. Complete both API
and model enablement before bootstrap, pack installation, or range
qualification; otherwise deployment can succeed while participant model calls
fail with a misleading model-not-found response.

### 2. Apply the foundation

Run `scripts/bootstrap/deploy.py gcp-foundation --inputs <file> --yes`. The
inputs select the mode:

- **Without GitHub:** start from
  `scripts/bootstrap/gcp-foundation.github-less.example.tfvars.json`. Name the
  principals that may impersonate the deploy, release-scan, and destroy
  identities, and the deployment config bucket. Follow
  [Apply the foundation without GitHub](../handbook/gcp-github-less-deployment.md#apply-the-foundation-without-github).
- **For CI:** start from `scripts/bootstrap/gcp-foundation.example.tfvars.json`.
  The command verifies the project and repository numeric IDs and requires
  explicit purpose Environment and workflow trust tuples. Follow the ordering in
  `scripts/bootstrap/README.md` and the staged cutover and readback in
  `docs/dev/deploy-secrets.md`.
- **For a developer build project:** start from
  `scripts/bootstrap/gcp-image-builder.example.tfvars.json`. The command creates
  the image build resources and writes the `gcp-image-build` and
  `gcp-image-validate` Environments of the GitHub repository. See
  [GCP Guest Images](../../architecture/gcp-guest-images.md#build-project).

Both modes create the state bucket before the identity Terraform initializes,
and both can create an isolated private image-build network. Do not hand-create
service accounts or broaden the provider condition.

For CI, add each output only to its matching GitHub Environment:

| Secret | Value |
|--------|-------|
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Deploy service account email |
| `GCP_RELEASE_SCAN_SERVICE_ACCOUNT` | Release-scan service account email |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | WIF provider resource name |

A developer build project does not need these steps. The bootstrap writes the
image Environments itself.

### 3. Configure the deployment inputs

- **Without GitHub:** write `bigrae.yaml` and `terraform.tfvars.json`, then
  publish them with `deploy.py gcp-config publish`. Create the first operator
  password in Secret Manager. Import the guest images that the release selects
  with `deploy.py gcp-images --store gs://<bucket>`. See
  [Author and publish the deployment input bundle](../handbook/gcp-github-less-deployment.md#author-and-publish-the-deployment-input-bundle).
- **For CI:** set the GitHub secrets and variables in
  [`docs/dev/deploy-secrets.md`](../../dev/deploy-secrets.md).

The command owns `project_id`, `region`, `public_hostname`, and the other
values that `bigrae.yaml` defines. Its values win over a conflicting Terraform
value. Keep committed placeholders synthetic; do not put real project
identities in the repository.

The model-access broker is parked and unsupported under ADR-068.
Use the supported keyless-direct guest model path where required.

### 4. Deploy

- **Without GitHub:** run the first deploy with `--first-install` from the
  deployer image:

  ```bash
  ./scripts/bootstrap/deploy.py gcp-deploy --store gs://<bucket> --first-install
  ```

- **For CI:** dispatch the deploy workflow:

  ```bash
  gh workflow run deploy.yml --ref <branch> -f environment=<environment>
  ```

  For the first bootstrap, set `gcp_require_active_certificate=false`.

The command applies the platform Terraform, creates the first Identity Platform
operator, copies and scans the release images, and verifies their provenance.
It then installs or upgrades the Helm release, runs the migrations, and waits
for the rollouts, the ingress address, and the certificate. It runs Terraform,
`kubectl`, and Helm as the deploy identity, with a private `KUBECONFIG`. See
[What the deploy does](../handbook/gcp-github-less-deployment.md#what-the-deploy-does) for the
stage list.

### 5. DNS and TLS

The GCP path requires a real hostname and managed TLS. Point the configured
hostname to the reserved global ingress IP so the Google-managed certificate can
become active. Then deploy again without `--first-install`; the command waits
for the certificate to become Active.

### 6. Optional: participant OpenVPN access

Participant OpenVPN (ADR-039-R10) is off by default. When it is on, every
deployment runs one shared, autoscaled OpenVPN server pool, and participants
download an `.ovpn` profile for their range. Both deploy paths create the pool:

1. Apply `platform/terraform/gcp/global/cicd-oidc` for the tenant's identity
   profile from a revision that includes the pool. The apply adds the deploy and
   destroy roles `<prefix>_deploy_vpn_pool` and `<prefix>_destroy_vpn_pool`. Each
   role covers the pool's instance templates, regional instance group, and
   autoscaler, and its VM, disk, and template permissions apply only to the
   pool's `shifter-<environment>-vpn-` names. Without these roles the deploy
   fails when it creates the pool's instance template. Confirm both bindings:

   ```bash
   gcloud projects get-iam-policy <project> --format=json \
     | jq -r '.bindings[] | select(.role | test("vpn_pool")) | .role'
   ```

2. Turn on the pool. Without GitHub, set
   `settings.participant_openvpn.enabled: true` in `bigrae.yaml` and publish the
   bundle. For CI, set the GitHub Environment variable
   `RANGE_OPENVPN_ENABLED=true`.
3. Run the deploy. It creates the pool, the tenant OpenVPN CA, and the pool
   server certificate. It also records the scanned image digest, starts the
   servers, and waits until every server is healthy behind the load balancer.
4. Plan quota for the pool: it adds `e2-standard-2` VMs (two vCPUs each) up to
   the capacity profile's maximum, plus one external address for the endpoint
   and one for the pool's NAT.

Do not set `openvpn_pool_enabled` in the Terraform values. The command sets it
from `settings.participant_openvpn.enabled`. Launches stay safe either way: the
runtime switch is off unless the deploy renders `RANGE_OPENVPN_ENABLED=true`,
and that render fails if no pool exists. See "Participant OpenVPN access" in
[`docs/dev/gcp-range-cell-deploy.md`](../../dev/gcp-range-cell-deploy.md) for
the network design, sizing, and session rules.
