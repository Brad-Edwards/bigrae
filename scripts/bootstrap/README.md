# Bootstrap Scripts

Bootstrap automation for Shifter infrastructure.

## Features

The `deploy.py` CLI provides an interactive walkthrough for bootstrapping a bare AWS or GCP account and deploying infrastructure with intelligent automation:

**Automated Steps (with confirmation):**
- GitHub secrets configuration (via `gh` CLI)
- Per-environment `.s3.tfbackend` file updates
- Git commit and push

**Manual Steps (external systems):**
- DNS record creation (ACM validation, ALB pointing)

**AWS Bootstrap Creates:**
- S3 bucket for Terraform state (with `use_lockfile = true` S3 native locking, no DynamoDB)
- GitHub OIDC provider for keyless CI/CD
- IAM role with all required permissions. The role uses an inline
  AdministratorAccess-equivalent policy so bootstrap works in AWS
  organizations that deny `iam:AttachRolePolicy` via SCP.
- Optionally deploys Terraform infrastructure

**GCP:**

- `gcp-foundation` applies the foundation: the state bucket, the purpose
  identities, the release-evidence bucket, and optionally the image-build
  network. Without GitHub, it also creates the deployment config bucket.
- `gcp-deploy`, `gcp-release-scan`, `gcp-smoke`, `gcp-destroy`, and `gcp-config`
  stand up, update, scan, smoke-test, and tear down one deployment from a
  published release, and manage its inputs (ADR-070). The control plane uses GKE
  and the shared Helm chart. GCE range cells are the default guest backend.
- `gcp-images` imports the base guest images from an explicit registry.
- `gdc-bootstrap` builds only the legacy GDC VM Runtime substrate. The following
  operations apply only to the `gdc` range backend:

- required GDC/GKE/GCP APIs and IAM bindings
- the custom VPC/subnet/firewall substrate for the eval cluster
- the Compute Engine admin workstation and cluster nodes
- the rendered `bmctl` cluster config and bootstrap bundle
- the hybrid GDC cluster plus VM Runtime enablement
- the inotify hardening needed to keep `macvtap-deviceplugin` stable
- admin workstation helpers for repeatable kubeconfig access
- the `shifter-gcp-dev-gdc-access` Secret Manager bundle consumed by the provisioner for GDC range-plane access

## Interactive Prompts

When automated options are available, you'll see:
```
[y/n/m]:
  y = yes (run automatically)
  n = no (abort - all steps are required)
  m = manual (show instructions and wait)
```

**Note:** All steps are mandatory for a functioning deployment. Choosing 'n' will abort the script with an explanation of why that step is required.

**Non-interactive (`--yes`).** Off a TTY (CI, an automation wrapper, a container),
the interactive prompts cannot be answered and the CLI would otherwise abort. Pass
`--yes` on `bootstrap`, `terraform`, `full`, or `account-recovery` to assume "yes"
for the routine confirmation prompts, so a fresh tenant can be stood up without a
terminal (issue #1639). `--yes` covers routine proceed prompts only. It does NOT
authorize destructive cleanup: the `account-recovery` sweep still requires its own
explicit `--sweep` flag. `--yes` is distinct from `--headless`, which governs only
the preflight prerequisite gate.

The CLI also sets `AWS_PAGER=""` for every `aws` call it runs, so the AWS CLI v2
pager never blocks bootstrap under a non-TTY or PTY.

## Commands

Bootstrap has no default GitHub repository (ADR-069). Commands that act on the
repository read the target from the `gh` default repository of this clone. Set
the default once in each clone with `gh repo set-default <owner>/<repo>`.
Bootstrap reads the repository and owner IDs from the GitHub API and shows the
identity before it writes. When no default is set, the command stops. The
`gcp-foundation` command reads the repository from its inputs file instead. The
GCP lifecycle commands and `gcp-images` use no GitHub service.

### GCP lifecycle commands (ADR-070)

| Command | Purpose |
| --- | --- |
| `gcp-deploy --store gs://<bucket>` | Stand up or update one deployment from the release manifest of the deployer image. Add `--first-install` until DNS points at the ingress address. |
| `gcp-release-scan --release-manifest <file> --project-id <project> --environment <environment>` | Scan the exact images of a release as the release-scan identity. |
| `gcp-smoke --store gs://<bucket>` | Run the advisory post-deploy range smoke. |
| `gcp-destroy --store gs://<bucket> --confirm <environment>` | Tear down the platform resources as the destroy identity. |
| `gcp-config publish\|pull\|show\|set-range-image --store gs://<bucket>` | Publish, pull, and show the deployment input bundle, and record one range image. |

Each lifecycle command reads a deployment input bundle from `--store
gs://<bucket>[/<prefix>]` or `--inputs <dir>`. By default, the command
impersonates the purpose identity of each stage (`--identity impersonate`). CI
uses `--identity ambient`, because the job credentials already are that
identity. `gcp-deploy` exits with status 3 when only the advisory smoke failed.
Run `deploy.py <command> --help` for every flag. The procedure is in
`docs/technical/handbook/gcp-github-less-deployment.md`. Failure handling is in
`docs/runbooks/rb023-gcp-deployment.md`.

## Fresh AWS Account Order

For a new AWS account, run bootstrap-only first. Do not start with `full`.
The self-hosted runner Terraform root uses the same S3 backend that
bootstrap creates, and the AWS deploy workflows cannot run until the
runners are provisioned and registered.

On a **re-used** account (one that previously ran Shifter and was torn down), run
`account-recovery --env <env> --profile <profile>` first to detect orphaned leftovers
from the prior teardown, and `--sweep` them once the report holds no surprises. A
brand-new account has no leftovers, so this is a no-op there. See the Account Recovery
command below.

1. Run `bootstrap --env dev --profile <profile>` to create the shared dev
   state bucket, GitHub OIDC provider, and deploy role. Let it set the
   `AWS_ROLE_ARN_DEV` and `TF_INFRA_STATE_BUCKET_DEV` GitHub secrets (the
   env-suffixed names the dev deploy workflows read; prod uses the unsuffixed
   `AWS_ROLE_ARN` / `TF_INFRA_STATE_BUCKET`) and update the dev `.s3.tfbackend`
   files.
2. Run `runners --env dev --profile <profile>` to provision the runner fleet
   **and** register it automatically (issue #1433). By default it provisions a
   dedicated, ADR-004-R20-compliant runner VPC (`create_runner_network`), applies
   `platform/terraform/global/github-runner`, mints a single-use token per runner,
   registers each over SSM, and verifies it online, with no manual `config.sh`. Pass
   `--use-existing-network` to place runners in an existing compliant network
   whose `vpc_id`/`subnet_id` you keep in a gitignored `local.auto.tfvars`
   instead of the standard managed runner VPC (ADR-004-R20, #1437). Registration tokens
   are never written to Terraform state, user data, a secret store, or logs.
3. Confirm the fleet is online (the `runners` path already verifies this):
   `REPO="$(gh repo set-default --view)"; gh api "repos/$REPO/actions/runners" --jq '.runners[] | {name, status}'`.
4. Seed or build the `/shifter/ami/{kali,ubuntu,windows,dc}` SSM
   parameters required by portal Terraform. The Kali build requires the target
   account to accept the free AWS Marketplace terms for product code
   `7lgvy7mt78lgoi4lant0znp5h`.
5. Configure the deployment tfvars secrets documented in
   `docs/dev/deploy-secrets.md`, including `TF_VARS_DEV_PORTAL` and
   `TF_VARS_DEV_RANGE`. Bootstrap configures the AWS role secret and backend
   files; deploy-time portal/range values live in those tfvars secrets for
   GitHub Actions, or in gitignored `local.auto.tfvars` files for local
   Terraform runs.
6. For the first deploy in the moved account, run the `Deploy` GitHub Actions
   workflow with `gh workflow run deploy.yml --ref <branch> -f environment=aws-dev`
   (add `-f aws_first_deploy=true` for the first-ever engine deploy). The
   dispatch forces the full AWS chain (Core -> Range -> Engine -> Platform).
   Deployment is always a manual dispatch selecting the `environment`; pushing to
   a branch runs validation only.
7. During that first platform apply, publish DNS records for ACM and SES
   validation in the authoritative DNS zone. ACM records come from the root
   Terraform output `acm_validation_records`. SES records come from
   `aws ses get-identity-verification-attributes` for the `_amazonses` TXT
   value and `aws ses get-identity-dkim-attributes` for the three DKIM CNAME
   tokens. In Cloudflare, keep ACM and DKIM CNAMEs DNS-only.
8. After platform apply creates runtime endpoints, publish routing records:
   `domain_name` and `chat.<domain_name>` CNAME to the root Terraform output
   `alb_dns_name`; `ctfd_domain` A-records to `ctfd_elastic_ip`.

The portal VPC creates private AWS service endpoints for the bootstrap-critical
services used by EKS workloads and retained range infrastructure: ECR, S3, CloudWatch
Logs, Secrets Manager, SSM, STS/KMS, ECS/EC2/ELB, SNS, SQS, and DynamoDB.
These endpoints are expected in fresh accounts, especially when portal
inspection is enabled, because the private default route traverses the
firewall/NAT path during image pulls, secret hydration and log delivery.

## Fresh GCP Account Order

GCP standup mirrors the AWS order: apply the foundation first, then prepare the
inputs and images, then deploy. A deployment without GitHub follows
`docs/technical/handbook/gcp-github-less-deployment.md`. The CI path follows the steps below and
the GCP Deployment section of `docs/technical/dev/setup.md`.

> **Before starting:** In the target GCP project, enable every Google Cloud API
> required by the selected Shifter configuration and enable every configured AI
> model in Vertex AI Model Garden. Model enablement is separate from enabling
> the Vertex AI API: an authorized project administrator must open each required
> model, accept any provider or Marketplace terms, and confirm that the model is
> available in its configured region. Complete both API and model enablement
> before bootstrap, pack installation, or range qualification; otherwise a
> deployment can succeed while participant model calls fail with a misleading
> model-not-found response.

For a deployment without GitHub, copy
`gcp-foundation.github-less.example.tfvars.json` to an operator-owned file
outside the repository. It names the principals that may impersonate the
deploy, release-scan, and destroy identities, and the deployment config bucket.
Do not set `purpose_contexts` or any GitHub input in that file; the command
rejects them in this mode.

For a CI deployment, copy `gcp-foundation.example.tfvars.json` to an
operator-owned file outside the repository. Supply the project ID and number,
numeric GitHub repository and owner IDs, bucket names, and exact purpose
Environment/branch/workflow tuples. Obtain IDs with `gcloud projects describe`
and `gh api repos/<owner>/<repo>`; bootstrap verifies them before writes.
Deploy uses the selected tenant branch. A destroy tuple must bind protected
`refs/heads/dev` or `refs/heads/main`; bootstrap rejects any other destroy tuple
before writes. No GitHub workflow uses a destroy identity after #2668. No secret
payload belongs in this file.

For a developer build project (#2668), copy
`gcp-image-builder.example.tfvars.json` to an operator-owned file outside the
repository. It has only the `build` and `validate` contexts. Each context names
the `gcp-image-build` or `gcp-image-validate` Environment, protected
`refs/heads/dev` or `refs/heads/main`, and the `packer-gcp.yml` workflow ref.
Bootstrap rejects any other image context before writes. After the apply, it
writes the two Environments of the GitHub repository with their branch
policies, secrets, and variables. See `docs/architecture/gcp-guest-images.md`.

```bash
./scripts/bootstrap/deploy.py gcp-foundation --inputs /path/to/foundation.tfvars.json --dry-run
./scripts/bootstrap/deploy.py gcp-foundation --inputs /path/to/foundation.tfvars.json --yes
```

This enables the foundation APIs, creates the private versioned state bucket,
and applies the independently owned `cicd-oidc` root from a saved plan under
operator credentials. Without GitHub, the root creates no Workload Identity pool
or provider, and it creates the deployment config bucket. The evidence bucket
has an irreversible 90-day retention lock. `enable_image_build_network=true` creates a foundation-owned custom VPC,
private subnet, Cloud NAT, and IAP-only build/validation ingress before any
platform deployment. It has no peering to runner or runtime networks. For a
developer build project, bootstrap writes its network and subnet outputs to the
image Environments as `GCP_PACKER_NETWORK` and `GCP_PACKER_SUBNETWORK`, and sets
`GCP_PACKER_USE_INTERNAL_IP=true` in the build Environment.
`enable_image_export_storage=true` creates the export storage that the build
workflow uses. Existing foundations keep this network disabled by default; their
state addresses and existing image network remain unchanged.

The image-build network's Terraform resources live in
`platform/terraform/gcp/modules/image-build-network`, instantiated by the
foundation root. Six `moved` blocks keep existing networks in the same
`cicd-oidc` state when upgrading from the former root-level resource addresses.
For an existing tenant that needs the Linux management SSH port, put its
reviewed, non-secret foundation input JSON in the deployment input bundle as
`foundation-inputs.json`, and run `gcp-deploy --reconcile-foundation-image-network`.
A CI deployment sets the tenant Environment variable
`GCP_FOUNDATION_INPUTS_JSON` instead, then manually dispatches **Deploy** from
that tenant branch with its matching `environment` and
`gcp_reconcile_foundation_image_network=true`. The command uses the existing
deploy identity to apply only an action-allowlisted saved plan; all other
foundation resources must remain no-op. The stage checks the live
image-build network and firewall before and after, retains the existing
`cicd-oidc` backend, and is skipped for ordinary deployments. Its plan uses
`-refresh=false` so the deploy identity does not need read access to the
foundational Workload Identity pool; the live image-build resources are checked
separately. Do not use this narrowly gated migration stage for general
foundation identity changes.

The CI path:

No tenant uses the CI path after #2668. `deploy.yml` offers no GCP environment
until a tenant is wired in, and #2672 decides the future of this path.

1. Create the GCP project and enable the required APIs.
2. Apply the foundational OIDC/WIF identity root
   (`platform/terraform/gcp/global/cicd-oidc`) to create the GitHub Actions
   Workload Identity pool/provider and purpose-scoped CI service accounts. Apply
   selected deployment values in each project. Each project keeps its own state bucket/prefix. Then set each
   purpose Environment's explicit service-account secret and
   `GCP_WORKLOAD_IDENTITY_PROVIDER` from the applicable profile outputs. This root has its own state prefix, separate from the
   platform root, so a platform destroy never removes the credentials CI
   authenticates as (it mirrors the `global/github-runner` containment):

   ```bash
   cd platform/terraform/gcp/global/cicd-oidc
   REPO="$(gh repo set-default --view)"   # Empty: run gh repo set-default <owner>/<repo> first.
   mapfile -t GITHUB_IDS < <(gh api "repos/$REPO" --jq '.id, .owner.id')
   terraform init -backend-config="bucket=<project-id>-terraform-state" -backend-config="prefix=cicd-oidc"
   terraform apply -var="project_id=<project-id>" -var="project_number=<project-number>" \
     -var="environment=<environment>" -var="name_prefix=<name-prefix>" \
     -var="github_org=${REPO%/*}" -var="github_repo=${REPO#*/}" \
     -var="github_repository_id=${GITHUB_IDS[0]}" -var="github_owner_id=${GITHUB_IDS[1]}" \
     -var="terraform_state_bucket_name=<project-id>-terraform-state" \
     -var="release_evidence_bucket_name=<release-evidence-bucket>" \
     -var-file=/path/to/purpose-contexts.tfvars.json \
     -var='platform_external_bucket_names=["<raes-package-bucket>","<ctf-content-bucket>"]'
   terraform output -raw workload_identity_provider          # GCP_WORKLOAD_IDENTITY_PROVIDER
   terraform output -raw release_scan_service_account_email     # GCP_RELEASE_SCAN_SERVICE_ACCOUNT
   terraform output -raw deploy_service_account_email           # GCP_DEPLOY_SERVICE_ACCOUNT
   terraform output -raw release_evidence_bucket_name           # private raw evidence store
   ```
   `purpose-contexts.tfvars.json` holds only the `purpose_contexts` key, in
   the shape of `gcp-foundation.example.tfvars.json`.
   Omit empty optional bucket entries. The external bucket set must exactly
   cover any non-empty `raes_package_bucket_name` and
   `ctf_content_bucket_name` used by platform-core so deploy/destroy can manage
   workload IAM on those exact resources without project-wide Storage Admin.
   Publish each purpose output only to its matching protected Environment. Existing installations follow the staged, state-address
   preserving cutover in `docs/dev/deploy-secrets.md` rather than applying the
   strict identity split from the shared CI principal.
3. Configure the GCP deployment secrets and variables in
   `docs/dev/deploy-secrets.md` (the `gcp-dev` section), including
   `SHIFTER_CONFIG_GCP_DEV` and the GCE range-cell variables.
4. Import the guest images that the release selects and print their exact
   references:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-images \
     --project-id <project> --region <region> \
     --release-manifest <release.json> --trusted-repository-id <id> --trusted-owner-id <id>
   ```

   Set the printed references as the `GCP_RANGE_LINUX_IMAGE`,
   `GCP_RANGE_KALI_IMAGE`, and `GCP_RANGE_DC_IMAGE` variables. A deployment
   without GitHub passes `--store gs://<bucket>` instead. The command then reads
   the trust from the store and writes `range-images.json`. Without
   `--release-manifest`, it reads the release manifest of the deployer image. The
   command verifies the signed build provenance of all three images before it
   changes anything. It imports a missing image through a private per-run bucket
   in the selected project and region, removes that bucket and every transfer
   object on success or failure, and requires all three native GCE images to be
   `READY`. A retry reuses matching `READY` images and creates no bucket when no
   import is necessary. Add `--dry-run` to verify and inspect without pulling
   disk payloads or mutating GCP.

   A developer build of `packer-gcp.yml` publishes a guest image only after
   `packer-gcp-validate.yml` passes for the exact candidate. See
   `docs/dev/gcp-range-cell-deploy.md` and
   `docs/architecture/gcp-guest-images.md`.
5. Run the first deploy through CI as a manual dispatch with
   `gcp_require_active_certificate=false`:
   `gh workflow run deploy.yml --ref <branch> -f environment=<environment> -f gcp_require_active_certificate=false`.
   The CI adapter renders the deployment input bundle from the GitHub values,
   and the jobs run `gcp-deploy`, `gcp-release-scan`, and `gcp-smoke`. The deploy
   applies the GCP Terraform (GKE, Cloud SQL, Memorystore, Pub/Sub), builds and
   attests the control-plane images, migrates the database and registers the
   shipped scenario catalog, then installs the Shifter Helm release.
6. Subsequent deploys run through CI as a manual dispatch:
   `gh workflow run deploy.yml --ref <branch> -f environment=<environment>` (see
   the CI/CD trigger matrix in `docs/technical/dev/ci-cd.md`). Branch names no
   longer trigger deploys.
7. Point the configured hostname at the reserved global ingress IP so the
   Google-managed TLS certificate can activate.

### Preflight (validate prerequisites, no changes)
```bash
./scripts/bootstrap/deploy.py preflight --cloud aws --env dev
./scripts/bootstrap/deploy.py preflight --cloud aws --env dev --component portal
./scripts/bootstrap/deploy.py preflight --cloud gcp --env <environment> --store gs://<bucket>
# --headless : non-interactive; fail on any missing required prerequisite, no prompts
```
The shared, fail-safe prerequisite gate (`preflight.py`): checks tools, secrets,
and config and reports every gap up front before any change. The same checks run
in the CI deploy workflows. `bootstrap`, `terraform`, and `full` run it
automatically at the start; it is interactive by default (auto-headless off a
TTY). For GCP, the local preflight checks the deploy tools and validates the
deployment input bundle from `--store` or `--inputs`. In CI, the first Identity
Platform operator credentials are required unless
`SHIFTER_SKIP_OPERATOR_BOOTSTRAP=true` is set (the skip is logged, never silent).
See `docs/dev/deploy-secrets.md`.

### Account Recovery (fresh-account leftover detect / sweep)
```bash
# Read-only detection (default): report leftovers that would block a fresh apply.
./scripts/bootstrap/deploy.py account-recovery --env dev --profile <your-dev-profile>

# Delete the owned leftovers (explicit destructive opt-in).
./scripts/bootstrap/deploy.py account-recovery --env dev --profile <your-dev-profile> --sweep

# Preview the sweep without deleting anything.
./scripts/bootstrap/deploy.py account-recovery --env dev --profile <your-dev-profile> --sweep --dry-run
```
When an account is re-used after an incomplete prior teardown, orphaned control-plane
residue collides with a fresh `terraform apply` one resource at a time
(`AlreadyExists` / `ResourceAlreadyExistsException`). This is the #1472 leftover set.
`account-recovery` finds it up front instead of collision-by-collision (issue #1639 / #1618).

Detection always runs first and is read-only. It reports these state-absent classes:
ECR repositories, Network Firewall rule groups (deleted asynchronously, so the sweep
polls to convergence), EventBridge Scheduler schedules, RDS event subscriptions, RDS
DB parameter groups, KMS aliases, portal SSM parameters under `/shifter/<env>/portal`,
and AWS Budgets.

Safety model:
- **Live-tenant refusal.** If a running tenant still occupies the environment (a portal
  ASG carrying instances, or an RDS instance for the env), the tool refuses entirely and
  tells you to `terraform destroy` first. It fails closed: if it cannot confirm the
  account is torn down, it treats the account as live.
- **Name plus ownership.** A resource is a candidate only when its name matches the
  environment naming AND its ownership tags agree (`Project=shifter`,
  `Environment=<env>`). A canonically named resource whose tags disagree is surfaced as
  `blocked` and never deleted; remove those by hand after review.
- **No data-bearing deletes.** Only the residue classes above are ever touched. KMS
  keys, S3 buckets, databases, snapshots, and secret values are never deleted (the tool
  deletes KMS aliases, not keys, and RDS parameter groups, not instances).
- **Explicit destructive opt-in.** Detection never deletes. The sweep needs `--sweep`
  AND a confirmation naming the account; `--yes` satisfies the confirmation but does not
  replace `--sweep`, so a non-TTY run alone never deletes.
- **Fail loud.** If any discovery call errors, detection is incomplete, so the tool
  reports the failure and refuses to sweep rather than certify a partial picture. Deletes
  are idempotent, so a partial or retried run is safe.

The following fresh-apply blockers are NOT yet automated and remain in the manual sweep
in `docs/dev/aws-teardown-runbook.md`: RDS DB subnet groups, the ElastiCache subnet
group, EC2 key pairs, and security groups.

### Bootstrap Only
```bash
./scripts/bootstrap/deploy.py bootstrap --env prod --profile <your-prod-profile>
```

### Terraform Only (after bootstrap)
```bash
./scripts/bootstrap/deploy.py terraform --env prod --profile <your-prod-profile>
```

#### GCP: running Terraform locally needs an ADC quota project

A GCP root that manages Identity Platform (`google_identity_platform_config`)
fails under local Application Default Credentials with a 403 whose `reason` is
`SERVICE_DISABLED`, even when `identitytoolkit.googleapis.com` is enabled:

```
Error when reading or editing IdentityPlatformConfig "projects/<project>/config":
googleapi: Error 403: Your application is authenticating by using local
Application Default Credentials. The identitytoolkit.googleapis.com API
requires a quota project, which is not set by default.
```

The message is misleading: the API is enabled, but user ADC carries no billing
or quota project, and `identitytoolkit` refuses to serve without one. CI does
not hit this because it authenticates through Workload Identity Federation with
a service account, which supplies its own quota project.

Set the quota project on ADC **and** tell the provider to use it. Both are
required: setting the quota project alone does not change provider behavior.

```bash
gcloud auth application-default set-quota-project <gcp-project>

export USER_PROJECT_OVERRIDE=true
export GOOGLE_BILLING_PROJECT=<gcp-project>
terraform plan   # or apply / destroy
```

This applies to any local `terraform plan`, `apply`, or `destroy` against a GCP
environment root, including teardown.

### Runners (provision + auto-register self-hosted runners)
```bash
# AWS (default): runners over SSM, into the account --profile authenticates to.
./scripts/bootstrap/deploy.py runners --env dev --profile <your-dev-profile>
# --use-existing-network : use vpc_id/subnet_id from local.auto.tfvars instead of the managed runner VPC
# --runner-count N       : override runner_count for this apply
# --dry-run              : show the plan without minting a token or sending SSM commands

# GCP (issue #1546): GCE runner into the target GCP project, over IAP (no SSM).
# Uses the operator's default gcloud/ADC identity (no --profile).
./scripts/bootstrap/deploy.py runners --cloud gcp --env <environment> --project-id <gcp-project>
# --region / --zone      : GCP region/zone (default us-central1 / us-central1-a)
# --labels               : custom runner label(s) (default: the env name)
```
Provisions the runner fleet (dedicated runner VPC by default) and registers each
runner end-to-end. Registration tokens are minted per runner and never persisted
to Terraform state, instance metadata/user data, a secret store, or logs
(issue #1433 for AWS/SSM; issue #1546 for GCP, where the token is delivered over
the `gcloud compute ssh` stdin stream and the runner registers with its
environment label so it never joins the AWS `self-hosted` pool). The GCP path fails closed
unless each runner is online with the expected label.

### Full Deployment (bootstrap + terraform)
```bash
./scripts/bootstrap/deploy.py full --env prod --profile <your-prod-profile>
```

### Dry Run (preview without changes)
```bash
./scripts/bootstrap/deploy.py full --env prod --profile <your-prod-profile> --dry-run
```

### Bootstrap a Repeatable GDC VM Runtime Cluster

`gdc-bootstrap` builds only the legacy GDC VM Runtime substrate for the `gdc`
range backend. It deploys no control plane; `gcp-deploy` deploys the control
plane for every range backend.

```bash
./scripts/bootstrap/deploy.py gdc-bootstrap --project-id example-gcp-project --cluster-id cluster1
```

This follows the official Google Distributed Cloud on Compute Engine evaluation path, but bakes in the
repo-specific fixes from the live spike:
- custom VPC/subnet instead of relying on `default`
- `multipleNetworkInterfaces: true` in the cluster config
- a deterministic `vxlan0` underlay for later shared-L2 scenario networks
- the persistent inotify sysctl fix before VM Runtime workloads are enabled

## Options

- `--env` (required): `dev` or `prod` (the deployment name for the `preflight --cloud gcp` path)
- `--profile` (required): AWS CLI profile name
- `--dry-run` (optional): Show what would happen without making changes
- `--cloud` (preflight only): `aws` or `gcp`
- `--component` (preflight only): `core`, `range`, or `portal` to scope AWS overlay checks
- `--store` / `--inputs` (GCP preflight only): the deployment input bundle to validate
- `--headless` (bootstrap/terraform/full/preflight): non-interactive; fail on missing prerequisites without prompting (auto-detected off a TTY)
- `--yes` (bootstrap/terraform/full/account-recovery): non-interactive; assume "yes" for routine confirmation prompts so the flow runs without a TTY. Does not authorize the destructive sweep (issue #1639)
- `--sweep` (account-recovery only): delete the owned leftovers found by detection (explicit destructive opt-in; detection is read-only without it)
- `--use-existing-network` (runners only): Place runners in an existing compliant network (`vpc_id`/`subnet_id` from a gitignored `local.auto.tfvars`) instead of provisioning the standard dedicated runner VPC
- `--runner-count` (runners only): Override `runner_count` for this apply
- `--project-id` (GDC only): GCP project ID, defaults to `PANW_GCP_DEV` or repo-root `.env`
- `--cluster-id` (GDC only): Cluster name / asset prefix, defaults to `cluster1`
- `--google-account-email` (GDC only): Optional Google identity to grant cluster-admin in cluster YAML

## Help

```bash
./scripts/bootstrap/deploy.py --help
./scripts/bootstrap/deploy.py preflight --help
./scripts/bootstrap/deploy.py account-recovery --help
./scripts/bootstrap/deploy.py bootstrap --help
./scripts/bootstrap/deploy.py terraform --help
./scripts/bootstrap/deploy.py runners --help
./scripts/bootstrap/deploy.py full --help
./scripts/bootstrap/deploy.py gdc-bootstrap --help
./scripts/bootstrap/deploy.py gcp-foundation --help
./scripts/bootstrap/deploy.py gcp-images --help
./scripts/bootstrap/deploy.py gcp-deploy --help
./scripts/bootstrap/deploy.py gcp-destroy --help
./scripts/bootstrap/deploy.py gcp-config --help
```

## Shared deployment configuration and retained callers

GCP platform Terraform runs from `platform/terraform/gcp/environments/platform`.
`gcp-deploy` passes the bundle's Terraform values and its generated var files explicitly.
Each run uses a private `TF_DATA_DIR` and the existing `shifter/<environment>/platform-core` state prefix.
AWS production roots remain owned by @Brad-Edwards and are called by `terraform`/`full` and the EKS lifecycle.
The optional portal CTFd module retains that owner and its portal-root caller.
See `docs/dev/deployment-configuration.md` for state selection and archived cleanup procedures.
