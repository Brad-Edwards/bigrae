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
- **GCP**: `gcloud` CLI authenticated with appropriate project

### Accounts and access

- A cloud account (AWS or GCP) with billing enabled and permissions to create
  IAM, networking, compute, database, and storage resources.
- Admin access to the GitHub repository to set Actions secrets and variables
  (Settings, then Secrets and variables, then Actions).
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

- A GCP project with the required APIs enabled.
- Workload Identity Federation configured for GitHub Actions (pool, provider,
  purpose service accounts), with the explicit `GCP_*_SERVICE_ACCOUNT` value
  and `GCP_WORKLOAD_IDENTITY_PROVIDER` set in each purpose Environment.
- Public Kali/Ubuntu/DC base packages available in GHCR. The first local
  `gdc-bootstrap --import-public-base-images` invocation imports them as native
  GCE images before platform preconditions run; no platform-owned transfer
  bucket must already exist. See
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

Use `--backend gcp` for GCP. See [Installation Config](installation-config) for the
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

# Validate the GCP environment
./scripts/bootstrap/deploy.py preflight --cloud gcp --env gcp-dev
```

The `bootstrap`, `terraform`, and `full` commands run the preflight automatically
at the start. It is interactive by default: it prints the results and asks you to
confirm the manual prerequisites it cannot verify (cloud authentication, DNS
access). Pass `--headless` (or run without a TTY, as CI does) to skip the prompts
and fail on any missing required prerequisite instead.

The first Identity Platform operator credentials
(`GCP_BOOTSTRAP_ADMIN_EMAIL` / `GCP_BOOTSTRAP_ADMIN_PASSWORD`) are required. To
deliberately skip operator creation, set `SHIFTER_SKIP_OPERATOR_BOOTSTRAP=true`
(locally) or the matching repository variable (CI); the skip is logged, never
silent.

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

1. Add its value file under `gcp/environments/<environment>/`.
2. Add its value-only Kubernetes overlay using the shared runtime component.
3. Configure its protected GitHub Environments and purpose identities.
4. Add its explicit workflow authorization choice.
5. Use a separate state bucket/prefix and `TF_DATA_DIR`.

Do not copy the shared Terraform source or runtime patches.
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

GCP provisions with Terraform (`platform/terraform/gcp/`, rooted at `environments/platform`, whose main module is `modules/platform-core/`) and a control plane deployed either from the Helm chart `platform/charts/shifter/` (the local `gdc-bootstrap` path) with generated Kubernetes deployment assets from `platform/k8s/gcp/overlays/<environment>/`.

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

### 2. Configure Workload Identity Federation

On a new project, run `scripts/bootstrap/deploy.py gcp-foundation --inputs
/path/to/foundation.tfvars.json --yes` using the complete example and ordering
in `scripts/bootstrap/README.md`. This creates the backend before identity
Terraform initializes and can provision an isolated private image-build network
before the platform exists. It verifies project/repository numeric IDs and
requires explicit purpose Environment and workflow trust tuples.

Apply `platform/terraform/gcp/global/cicd-oidc` with each deployment’s explicit
identity values and follow the staged cutover/readback in
`docs/dev/deploy-secrets.md`. Do not hand-create service accounts or broaden the
provider condition. The profiles create one provider per project and distinct
purpose identities:

1. Create and protect the purpose GitHub Environments.
2. Apply the purpose identities from an independent operator principal.
3. Add each output only to its matching Environment:

| Secret | Value |
|--------|-------|
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Deploy service account email |
| `GCP_DESTROY_SERVICE_ACCOUNT` | Destroy service account email |
| `GCP_PACKER_BUILD_SERVICE_ACCOUNT` | Image build service account email |
| `GCP_PACKER_VALIDATE_SERVICE_ACCOUNT` | Image validation service account email |
| `GCP_PACKER_PROMOTE_SERVICE_ACCOUNT` | Image promotion service account email |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | WIF provider resource name |

### 3. Configure deployment-specific values

The committed `terraform.tfvars` ships an `example.com` baseline; supply
the real values via a gitignored environment-local `local.auto.tfvars`.
Bootstrap passes this file explicitly to the shared root:

```bash
cat > platform/terraform/gcp/environments/gcp-dev/local.auto.tfvars <<'EOF'
project_id                  = "<your-gcp-project-id>"
dynamic_secret_project_id   = "<your-gcp-project-id>"
public_hostname             = "shifter.<your-domain>"
enable_managed_tls          = true
gke_master_authorized_cidrs = []
EOF
```

Treat an existing `local.auto.tfvars` as deployment state, not as a reusable
sample. Before bootstrap, replace every tenant-bound value left by an earlier
project, especially `project_id`, `dynamic_secret_project_id`, and
`public_hostname`. Bootstrap loads the selected file and its values drive the
ingress and managed certificate; selecting a different `bigrae.yaml` does not
override a stale hostname here. Confirm the hostname in both inputs agrees
before applying so bootstrap does not stop at DNS/TLS for the previous tenant.

For CI deploys the equivalent values come from GitHub secrets; see
[`docs/dev/deploy-secrets.md`](../../dev/deploy-secrets.md).

The CI deployment reads the selected value-only Kubernetes overlay.
The shared component supplies runtime patches.
`scripts/gcp/render_overlay_identity.py` supplies image repositories and workload identities from Terraform outputs.
Keep committed placeholders synthetic; do not put real project identities in these files.

The model-access broker is parked and unsupported under ADR-068.
Use the supported keyless-direct guest model path where required.

### 4. Deploy

The first clean install runs locally under your own credentials (Workload Identity
Federation is only needed for CI). Subsequent deploys run through CI with
`gh workflow run deploy.yml --ref dev -f environment=gcp-dev`. The local
bootstrap entrypoint is below. GitHub Environment variables are not imported into
the operator's shell, so export the range-plane values locally even when CI is
already configured. Refresh both the `gcloud` user credential and Application
Default Credentials before a long bootstrap, and verify the Artifact Registry
Docker credential helper is on `PATH`:

```bash
gcloud auth print-access-token >/dev/null
gcloud auth application-default print-access-token >/dev/null
command -v docker-credential-gcloud >/dev/null

export RANGE_NETWORK_ZONE=<zone>
export GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL=<range-host-service-account>
```

Use the exact Terraform identity, not a hand-normalized variant of the
environment name. The standard module removes hyphens from the
`shifter-<environment>` account-id prefix (for example, `gcp-dev` becomes the
prefix `shiftergcpdev`). After the first platform apply, read back the canonical
value:

```bash
TF_DATA_DIR="$PWD/platform/terraform/gcp/environments/<environment>/.terraform" \
  terraform -chdir=platform/terraform/gcp/environments/platform \
  output -raw range_host_service_account_email
```

Publish that exact value to the GitHub Environment and reuse it for local
retries.

If `gcloud` is installed under `~/google-cloud-sdk` but the credential-helper
check fails, load the SDK's standard path initializer and add the same guarded
source line to the operator's shell startup file before running bootstrap:

```bash
if [ -f "$HOME/google-cloud-sdk/path.bash.inc" ]; then
  . "$HOME/google-cloud-sdk/path.bash.inc"
fi
command -v docker-credential-gcloud >/dev/null
```

Do not work around a missing helper with a one-off registry login. Bootstrap
builds and pushes several images, and the persistent SDK path initialization
keeps the documented credential-helper flow available on later deploys.

When exact first-operator credentials are supplied in the process environment,
set `SHIFTER_BOOTSTRAP_ENV_SOURCE=process` with
`GCP_BOOTSTRAP_ADMIN_EMAIL` and `GCP_BOOTSTRAP_ADMIN_PASSWORD`. This prevents a
file-backed value from taking precedence unexpectedly. Do not set
`SHIFTER_SKIP_OPERATOR_BOOTSTRAP` unless omitting the first human operator is an
explicit deployment decision.

The public DC artifact can require substantially more temporary space than a
memory-backed `/tmp`. Set `TMPDIR` to a filesystem with enough free space for the
largest compressed and expanded image before using `--import-public-base-images`.

Then run:

```bash
./scripts/bootstrap/deploy.py gdc-bootstrap \
  --project-id <your-gcp-project-id> --environment <environment> \
  --region <region> --zone <zone> \
  --bigrae-config /path/to/bigrae.yaml \
  --import-public-base-images --yes
```

Keep the kubeconfig current context pinned to this tenant for the entire local
bootstrap, including the DNS/TLS wait. The bootstrap obtains the target Connect
Gateway context itself, but subsequent `kubectl` polls use the shared current
context. Do not run another cluster's `get-credentials` or `use-context` against
the same kubeconfig concurrently; use a separate `KUBECONFIG` for parallel
cluster work.

Despite the command name, the default `--range-backend gce` deploys the GKE control
plane and the GCE range plane and skips the GDC/ABM VM Runtime substrate. That
substrate is built only with `--range-backend gdc`. With the default
`--terraform-identity operator-adc`, Terraform runs under your Application Default
Credentials, creating no service account or key. The flow:

1. validates GCP/GitHub authentication and all three public OCI manifests
2. imports or reuses exact digest-derived `READY` GCE images through a private,
   per-run transfer bucket that is removed on every exit
3. publishes the exact references to the selected GitHub Environment and binds
   those same values to the local platform bootstrap
4. applies GCP Terraform (GKE, Cloud SQL, Memorystore, Pub/Sub, and related resources)
5. seeds the first Identity Platform operator
6. builds and pushes control-plane images
7. renders secure Helm values from Terraform outputs and Secret Manager
8. installs or upgrades the BigRAE Helm release

For an existing platform, refresh only the public base set with
`./scripts/bootstrap/deploy.py gcp-images --project-id <project> --environment
<environment> --region <region>`. Either command accepts `--dry-run`; discovery
and read-only validation still run, while payload download and GCP/GitHub
mutation do not.

With `--range-backend gdc`, the flow first builds or reconciles the GDC substrate.

### 5. DNS and TLS

The GCP path requires a real hostname and managed TLS. Point the configured
hostname to the reserved global ingress IP so the Google-managed certificate can
become active.

### 6. Optional: participant OpenVPN access

Participant OpenVPN (ADR-039-R10) is off by default. When it is on, every
deployment runs one shared, autoscaled OpenVPN server pool, and participants
download an `.ovpn` profile for their range. The local `gdc-bootstrap` path never
deploys the pool, so turn it on after bootstrap through the CI deploy:

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

2. Set the GitHub Environment variable `RANGE_OPENVPN_ENABLED=true` for the
   tenant.
3. Run the CI deploy:
   `gh workflow run deploy.yml --ref <branch> -f environment=<environment>`.
   It creates the pool, the tenant OpenVPN CA, and the pool server certificate.
   It also records the scanned image digest, starts the servers, and waits until
   every server is healthy behind the load balancer.
4. Plan quota for the pool: it adds `e2-standard-2` VMs (two vCPUs each) up to
   the capacity profile's maximum, plus one external address for the endpoint
   and one for the pool's NAT.

Do not set `openvpn_pool_enabled = true` in `local.auto.tfvars` for a local
bootstrap. The local path does not build the server image or write the PKI and
release record, so those servers never start. Launches stay safe either way: the
runtime switch is off unless the CI deploy renders `RANGE_OPENVPN_ENABLED=true`,
and that render fails if no pool exists. See "Participant OpenVPN access" in
[`docs/dev/gcp-range-cell-deploy.md`](../../dev/gcp-range-cell-deploy.md) for
the network design, sizing, and session rules.
