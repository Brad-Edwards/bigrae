# Deploy secrets and repository variables

Part of the BigRAE deploy and operations docs; start at the [documentation home](../index.md).

The committed `terraform.tfvars` files in `platform/terraform/environments/...`
and `platform/terraform/gcp/environments/...` ship account-neutral baselines
that are intentionally broken-on-deploy where real account values are
required. Each environment provides its real deployment values via GitHub
secrets, repository variables, or a local operator overlay; deploy workflows
write them into private temporary var-files. AWS retains its local override flow.

A GCP deployment without GitHub keeps its values in its deployment config
bucket and in Secret Manager. See
[GCP deployment inputs without GitHub](#gcp-deployment-inputs-without-github)
and the [GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md).
A GCP CI deployment keeps its values in GitHub. Its CI adapter renders them
into the same deployment input bundle, and the jobs run the same deploy
commands.

This file lists values that must be configured before a fresh deploy. Set values
under **Settings → Secrets and variables → Actions**, separated by:

- **Secrets**: sensitive (project IDs, public keys with identifying
  comments, alarm email addresses, allow-list domains for self-signup,
  CIDR blocks for operator access).
- **Variables**: non-sensitive deployment parameters (region selection,
  feature flags).

Required values are enforced by the shared deploy preflight
(`scripts/bootstrap/preflight.py`), which runs the same checks locally and in the
CI deploy workflows. It reports every missing prerequisite up front before any
Terraform apply, so a gap fails in seconds rather than mid-run. Validate an
environment at any time without making changes:

```bash
./scripts/bootstrap/deploy.py preflight --cloud aws --env dev
./scripts/bootstrap/deploy.py preflight --cloud gcp --env <environment> --store gs://<deployment-config-bucket>
```

For GCP, the local preflight checks the tools and validates the deployment
input bundle. The CI preflight checks the GitHub values. The **Required**
column in the tables below marks the secrets the CI preflight enforces. The first Identity Platform operator credentials are required unless
`SHIFTER_SKIP_OPERATOR_BOOTSTRAP=true` is set (locally) or the matching
repository variable is set (CI); the skip is logged, never silent.

## Secrets required to stand up an AWS environment

This is the single authoritative checklist of every secret a fresh AWS
environment standup needs, and how each one is populated. `dev` names are shown;
substitute `_PROD` (or the unsuffixed prod name) for the other AWS
environments. `.github/workflows/deploy.yml` forwards all of these to the
reusable deploy workflows. The GCP (`gcp-dev`) secrets are separate and listed
under [GCP (gcp-dev)](#gcp-gcp-dev).

The **Required** column marks the secrets the deploy preflight
(`scripts/bootstrap/preflight.py`) enforces before any Terraform apply; a
missing required secret fails the deploy up front rather than mid-run.

| Secret | Populated by | Required | Notes |
|---|---|---|---|
| `AWS_ROLE_ARN_DEV` | `scripts/bootstrap/deploy.py bootstrap --env dev` | yes | OIDC role the deploy jobs assume. Prod is `AWS_ROLE_ARN`. |
| `TF_INFRA_STATE_BUCKET_DEV` | `scripts/bootstrap/deploy.py bootstrap --env dev` | yes | Terraform state bucket the backend render reads. Prod is the unsuffixed `TF_INFRA_STATE_BUCKET`. |
| `TF_VARS_DEV_CORE` | `scripts/sync-deploy-secrets.sh --env dev` | yes | Core stack `local.auto.tfvars` payload (`budget_alert_email`). |
| `TF_VARS_DEV_RANGE` | `scripts/sync-deploy-secrets.sh --env dev` | yes | Range stack `local.auto.tfvars` payload (`agent_s3_bucket`, `vm_series_ami_id`). |
| `TF_VARS_DEV_PORTAL` | `scripts/sync-deploy-secrets.sh --env dev` | yes | Portal stack `local.auto.tfvars` payload (domain, email, buckets, capacity). |
| `TF_VARS_DEV_EKS` | protected environment secret | yes for EKS | JSON var-file for the isolated EKS root, including the closed pinned `addon_versions` object, `edge_client_cidrs`, provider API CIDRs, cluster inputs, and the canonical non-secret runtime projection. It does not carry a Load Balancer Controller policy ARN; that policy is module-owned. Prod uses the matching environment suffix. Apply the matching `platform/terraform/global/iam` stack before enabling EKS. The EKS job remains disabled until the matching repository variable (`AWS_EKS_DEV_ENABLED` or `AWS_EKS_PROD_ENABLED`) is set to `true` for a controlled migration or EKS-native standup. |
| `SHIFTER_CONFIG_DEV_RANGE` | `scripts/sync-deploy-secrets.sh --env dev --stack config --bigrae-config ./bigrae.yaml` | yes | Deployment `bigrae.yaml`; its `settings.range_egress` renders the range egress allowlist. |
| `SMOKE_TEST_USER_EMAIL` | manual | no | Post-deploy smoke user. See [Post-deploy smoke secrets](#post-deploy-smoke-secrets-dev). |
| `PLATFORM_BOOTSTRAP_STAFF_EMAILS` | manual | no | Comma-separated emails elevated to Django `is_staff` on first sign-in. Shared across all environments including prod. |
| `PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS` | manual | no | Comma-separated emails elevated to `is_superuser`. Shared across all environments including prod. |
| `SONAR_TOKEN` | manual | no | SonarCloud analysis token for the PR quality gate. Repository-wide, not per-environment. The project key and organization are not secrets and come from repository variables instead: see [SonarCloud project identity](#sonarcloud-project-identity). |
| `AWS_IMAGE_ROLE_ARN_DEV` | manual (from global-IAM output) | no | OIDC role the `packer.yml` base-image `build` job assumes (issue #1656), separate from the deploy `AWS_ROLE_ARN_*`. Least-privilege: its trust is pinned to the `dev`/`main` subjects and its `iam:PassRole` is scoped to the exact range instance role. Not checked by the deploy preflight, but the base build fails closed without it. Prod is `AWS_IMAGE_ROLE_ARN` (base builds target dev). Set from `terraform output -raw github_actions_image_role_arn` in `platform/terraform/global/iam`. See the [AWS AMI seeding runbook](aws-ami-seeding-runbook.md). |

"Populated by bootstrap" secrets are set once per account. "Populated by
`sync-deploy-secrets.sh`" secrets are re-pushed whenever the matching local
overlay changes. "Manual" secrets are set once under **Settings → Secrets and
variables → Actions** and are not written by either script.

Dev and prod range payloads are covered by the secret-sync script.

### SonarCloud project identity

Two repository **variables**, not secrets, identify the SonarCloud project
(ADR-003-R7). They are repository-wide, set once, and not written by either sync
script:

| Variable | Value | Notes |
|---|---|---|
| `SONAR_PROJECT_KEY` | `Brad-Edwards_shifter` | Carries the project's entire measure history and new-code baseline. The spelling and case must not change. A different key starts an empty project and loses the baseline. |
| `SONAR_ORGANIZATION` | `brad-edwards` | SonarCloud organization owning the project. |

The `SonarQube Cloud scan` step in `.github/workflows/_quality.yml` passes both to
the scanner; the values are public, so they are safe on the command line while
`SONAR_TOKEN` stays in the step's `env:`. Everything else the scan needs (sources,
exclusions, suppressions, coverage report paths) is shared analysis configuration
and stays committed in `sonar-project.properties`.

The scan is gated on `github.repository`, deliberately **not** on these variables
being set. A missing or renamed variable therefore fails the scanner and the job,
rather than skipping the scan and quietly removing the SonarCloud quality gate
from `PR Gate`.

Two cases skip the scan on purpose:

- **Runs under any other repository slug.** SonarCloud is this project's tooling
  choice, not a dependency imposed on anyone who cloned the repository and ran
  the workflow as-is. Their runs skip it; to analyse their own fork they set
  these two variables plus a `SONAR_TOKEN` of their own.
- **Fork-origin pull requests.** They execute in the base repository's context,
  so the identity test passes, but GitHub withholds secrets from them. Scanning
  would fail on an empty `SONAR_TOKEN` and give an outside contributor a red
  check they cannot fix. Their code is analysed on the `dev` run after merge, so
  nothing reaches a release unanalysed.

## Populating and syncing the secrets

The `TF_VARS_<ENV>_PORTAL` / `_RANGE` / `_CORE` and `SHIFTER_CONFIG_<ENV>_RANGE`
entries are whole-file payloads. Operators keep the
same values as gitignored `local.auto.tfvars` overlays (also used for local
`terraform` and symlinked into worktrees by `scripts/setup-worktree.sh`) and a
deployment `bigrae.yaml`. `scripts/sync-deploy-secrets.sh` pushes those local
files into the matching GitHub secrets with `gh secret set`, so the secret and
the local overlay stay in step instead of the secret being hand-edited in the
GitHub UI:

```sh
# Preview what would be set (no writes):
scripts/sync-deploy-secrets.sh --env dev --dry-run

# Sync the dev tfvars overlays (portal, range, core):
scripts/sync-deploy-secrets.sh --env dev

# Include the SHIFTER_CONFIG_* payloads rendered from a bigrae.yaml:
scripts/sync-deploy-secrets.sh --env dev --stack config --bigrae-config ./bigrae.yaml
```

The script does not sync GCP inputs. A GCP deployment without GitHub publishes
its inputs with `scripts/bootstrap/deploy.py gcp-config publish`. A GCP CI
deployment sets `SHIFTER_CONFIG_GCP_DEV` and the other GitHub values directly.

The script fails loud when a selected overlay file is missing and never prints
secret contents. `scripts/bootstrap` still owns the one-time IAM role secret
(`AWS_ROLE_ARN_*`) and the state-bucket secret; this script owns the recurring
per-env tfvars/config payloads. Run it after editing an overlay (for example,
after changing the ASG capacity in a portal overlay) so the next deploy picks up
the new values rather than a stale secret.

Both bootstrap-owned secret names are env-suffixed for every non-prod
environment and unsuffixed only for prod, matching what the deploy workflows
read: prod uses `AWS_ROLE_ARN` / `TF_INFRA_STATE_BUCKET`, while dev uses
`AWS_ROLE_ARN_DEV` / `TF_INFRA_STATE_BUCKET_DEV`. A fresh non-prod bootstrap
sets the suffixed names automatically; do not set the unsuffixed
`TF_INFRA_STATE_BUCKET` for a dev standup.

## GCP deployment inputs without GitHub

A GCP deployment without GitHub needs no GitHub secret, variable, or
Environment (ADR-070). Each input has one home outside GitHub. The commands
read them from the deployment input bundle in the deployment config bucket.
The full procedure is in the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md).

| Input | Home | Notes |
|---|---|---|
| Root installation config | `bigrae.yaml` | The project, the region, the domain, and the deployment settings. Secrets appear only as references. |
| Platform Terraform values | `terraform.tfvars.json` and/or `terraform.tfvars` | Network ranges, sizing, and the Identity Platform allow-list. When both exist, the JSON file wins. |
| Range guest images | `range-images.json` | Exact GCE references for the `linux`, `kali`, `windows`, and `dc` roles. `gcp-images --store` and `gcp-config set-range-image` write it. |
| First operator password | Secret Manager | `settings.operator_bootstrap.admin_password_secret_ref`, a versionless resource. The command reads the `latest` version. |
| Release trust anchor | `bigrae.yaml` | `settings.release_trust`: the numeric repository and owner IDs that sign the release. |
| Purpose identities | Naming contract | `<name prefix without hyphens>-deploy`, `-scan`, and `-destroy`. The default name prefix is `shifter-<environment>`. |
| Foundation reconcile inputs | `foundation-inputs.json` | Optional. Only for `gcp-deploy --reconcile-foundation-image-network`. |

### Initial secrets

The platform Terraform creates and fills these Secret Manager secrets: the
Django key and field encryption key, the database credentials, the Guacamole
keys, the Redis credentials, and the domain controller password. Do not create
them by hand.

Create only these secrets:

1. The first operator password. Create a secret in the deployment project and
   add the password as its first version. Set
   `settings.operator_bootstrap.admin_password_secret_ref` to
   `projects/<project>/secrets/<secret>`. The deploy identity reads the secret.
   Set `operator_bootstrap.enabled: false` to create no operator.
2. The transactional email API key, only when `email_backend` is set in the
   Terraform values. Terraform creates the empty `shifter-<environment>-email`
   secret. Add the provider API key to it.

The operator email must use the domain in `identity_allowed_email_domain`.
`gcp-deploy` checks it against the Terraform output before it creates the
operator.

## GCP (gcp-dev)

Consumed by `.github/workflows/_gcp-dev.yml` for a GCP CI deployment. The CI
adapter (`scripts/gcp/render_ci_deployment_inputs.py`) projects each value onto
its home in the deployment input bundle. Then the jobs run `gcp-deploy`,
`gcp-release-scan`, and `gcp-smoke`. A GitHub value that conflicts with a value
in `SHIFTER_CONFIG_GCP_DEV` stops the render, so each input keeps one meaning.

| Name | Kind | Required | Notes |
|---|---|---|---|
| `GCP_PROJECT_ID` | secret | yes | The Google Cloud project the platform deploys to. |
| `SHIFTER_CONFIG_GCP_DEV` | secret | yes | Full validated deployment `bigrae.yaml`. The CI adapter writes it as the bundle's `bigrae.yaml`. It is the sole source of `dynamic_secret_project_id`, exact provisioner static-secret refs, and range egress policy for deploy and destroy. |
| `SHIFTER_CONFIG_OVERLAY_JSON` | variable | no | Optional bounded JSON overlay for `settings.model_access`, `settings.model_broker`, and `settings.model_broker_runtime`. A reviewed `platform/deploy/gcp/<environment>/model-broker-overlay.template.json` takes precedence when present; the command binds its project and public CA, then applies it only to the private configuration copy. Without a template, the CI adapter applies this overlay to the bundle's `bigrae.yaml`. Arbitrary settings, root keys, and secrets are rejected. The broker is parked and not supported for use; leave this unset. See "Optional: model access broker" in `docs/technical/dev/setup.md`. |
| `GCP_REGION` | variable | no | Projected onto `settings.region`. Default `us-central1` when neither sets it. |
| `GCP_PUBLIC_HOSTNAME` | secret | yes | DNS name the platform serves on (for example, `shifter.your-domain.example`). Projected onto `deployment.domain`. |
| `GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN` | secret | yes | Identity Platform beforeCreate allow-list; the bootstrap operator must end with `@<this>` for sign-in to succeed. The CI adapter writes it to the bundle's `terraform.tfvars.json`, with `gke_master_authorized_cidrs = []` for the Connect Gateway path. |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | secret | yes | Purpose-scoped WIF service account for deploy and post-deploy smoke. |
| `GCP_RELEASE_SCAN_SERVICE_ACCOUNT` | secret | yes | Purpose-scoped WIF service account for the isolated exact-digest image scan. Store it only in `gcp-release-scan-dev`; it has repository-scoped Artifact Registry read and create-only access under the private bucket's `release-scans/` prefix, but no deployment or runtime-secret authority. |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | secret | yes | Workload identity provider resource id. |
| `GCP_BOOTSTRAP_ADMIN_EMAIL` | secret | yes | Optional Identity Platform account seed; this does not grant application permission. Must match `GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN`. Projected onto `settings.operator_bootstrap.admin_email`. Required unless `SHIFTER_SKIP_OPERATOR_BOOTSTRAP=true` is set to deliberately skip operator creation (the skip is logged; it sets `operator_bootstrap.enabled: false`). |
| `GCP_BOOTSTRAP_ADMIN_PASSWORD` | secret | yes | Initial password for the provider account seed; native application access still requires immutable bootstrap facts. The infrastructure stage reads it from this variable (`--operator-password-env`); it is never written to the bundle. Required unless `SHIFTER_SKIP_OPERATOR_BOOTSTRAP=true`. |
| `GCP_CTF_CONTENT_BUCKET` | variable | with content refs | GCS bucket holding digest-pinned native CTF content bundles; projected onto `settings.ctf_content_bucket`, rendered into the `ctf_content_bucket_name` tfvar, and surfaced to the runtime as `SHIFTER_CTF_CONTENT_BUCKET`. **Required whenever the app secret bundle carries `ctf_content_references`:** `config/_ctf_content_settings.py` fails closed when references are configured without a bucket, so leaving this unset renders a ConfigMap with no binding and crashloops the portal. The reference catalog itself stays in the app secret, never here. |
| `PLATFORM_BOOTSTRAP_STAFF_EMAILS` | secret | no | Legacy staff-email input; Django flags do not grant native application authority. Projected onto `settings.operator_bootstrap.staff_emails`. |
| `PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS` | secret | no | Legacy superuser-email input; Django flags do not grant native application authority. Projected onto `settings.operator_bootstrap.superuser_emails`. |
| `SMOKE_TEST_USER_EMAIL` | secret | no | Post-deploy smoke user for the advisory `post-deploy-smoke` job. Projected onto `settings.post_deploy_smoke.user_email`. The CI smoke step fails and opens the smoke issue when it is empty. Same contract as AWS dev smoke; see [Post-deploy smoke secrets](#post-deploy-smoke-secrets-dev). |
| `IDENTITY_CUTOVER_FACTS_JSON` | secret | first hard cut | Reviewed `shifter.identity-cutover-facts/v1` mapping document. CI writes a temporary owner-only file and passes its path as `--identity-cutover-facts`; the command mounts facts only into the one-shot cutover Job and cleans up afterward. |
| `IDENTITY_BOOTSTRAP_FACTS_JSON` | secret | first hard cut | `shifter.identity-bootstrap-facts/v1` names the exact immutable provider issuer and subject for the first native administrator. CI passes an owner-only file path as `--identity-bootstrap-facts`. Email lookup is not accepted. Both fact files are required together for the first cutover. |
| `CATALOG_CREDENTIAL` | secret | catalog setup | Current native bearer proof with installation content registration, conformance and publication authority. CI writes a temporary `0600` file, passes only its path as `--catalog-credential-file`, clears the environment value before the command and removes the file afterward. It never enters the deployment bundle. Missing proof leaves the activated application available but reports catalog setup incomplete; obtain a current credential after activation and retry. |

The prepare job scopes its shared preflight to the deployment Environment. The
exact-release scanner validates `GCP_RELEASE_SCAN_SERVICE_ACCOUNT` and its WIF
provider inside the separate `gcp-release-scan-<deployment>` Environment, so
the scanner identity never has to be copied into the deploy Environment.

No GitHub workflow tears down a GCP deployment after #2668. An operator runs
`deploy.py gcp-destroy` as the destroy identity (see the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md)).

GCP may report Cloud SQL, Memorystore, or GKE deleted before Service Networking
and load-balancer dependencies observe the release. `gcp-destroy` retries the
idempotent Terraform destroy with bounded backoff for this convergence; do not
delete the reported dependency manually or remove it from state.

`SHIFTER_CONFIG_GCP_DEV` is also required by both deploy and destroy. Its GCP
settings must include `dynamic_secret_project_id`; no separate GitHub variable
or tfvars override owns that value. The project is a pre-existing,
single-deployment Secret Manager boundary and may equal `project_id` only during
the documented expand phase. Once cut over it must be distinct.

Supported operator-created inputs are declared once in the same config:

```yaml
settings:
  project_id: platform-project
  dynamic_secret_project_id: range-secret-project
  provisioner_static_secret_refs:
    GDC_ACCESS_SECRET_ID: projects/platform-project/secrets/shifter-gcp-dev-gdc-access
    GDC_VM_IMAGE_GCS_SECRET_ID: projects/platform-project/secrets/shifter-gcp-dev-gdc-vm-image-gcs
    GDC_VMSERIES_IMAGE_GCS_SECRET_ID: projects/platform-project/secrets/shifter-gcp-dev-gdc-vm-image-gcs
```

Omit unused keys. Values are secret references, never payloads, and must identify
existing resources before Terraform applies. The renderer uses this closed map
for both exact provisioner IAM and runtime publication; do not duplicate the
same references in `local.auto.tfvars` or an ad-hoc runtime overlay. See the
[range-secret project rollout](../../platform/terraform/gcp/README.md#expand-cut-over-drain-contract).

Native CTF scenario-content references live in the existing application secret,
not in GitHub variables or the generated ConfigMap. Configure the private
bucket through the GCP Terraform input and follow the
[native CTF scenario-content runbook](ctf-scenario-content.md).

The foundational identity root also creates `<project>-release-evidence` with
public access prevention, uniform access, versioning, and an intentionally
locked 90-day retention policy. Locking is irreversible; confirm the project
and retention period in the reviewed Terraform plan before applying. Objects
age out after 365 days. IAM conditions separate every evidence class by object
prefix: build creates `packer-builds/` source-binding records; validation reads
only those build records and creates `packer-validation/` records;
release-scan creates `release-scans/`; and deploy creates `deployments/`. The
scan and deployment verdicts
(`verdict.json`, with an opaque locator and the evidence digest) are stored next
to their raw evidence; CI uploads no deploy verdict artifact. The image
validation verdict stays a redacted Actions artifact.

`gcp-deploy` validates the bootstrap operator email against the Terraform output
`identity_allowed_email_domain` before it creates the operator.

### GitHub Actions WIF trust (ADR-004-R23)

This procedure applies to a foundation with GitHub federation, which after #2668
is a developer build project. A deployment without GitHub sets an empty
`purpose_contexts`, so the module creates no Workload Identity pool or provider.
Its operators impersonate the purpose identities instead (see the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md#prerequisites-and-roles)).

The `cicd-oidc-identity` module federates GitHub Actions into GCP with an
**exact-subject** trust. The provider `attribute_condition` admits only this
repository and its immutable IDs, an exact protected `assertion.ref`, the exact
workflow, and an allow-listed `assertion.sub`. Each purpose identity is bound only
to its pairwise-disjoint purpose subjects, never to a repository-wide
`principalSet`.

1. **Verify the OIDC subject format first.** Run
   `REPO="$(gh repo set-default --view)"`. Then
   `gh api "repos/$REPO/actions/oidc/customization/sub"` must return
   `use_default: true`, `use_immutable_subject: true`. The `sub_claim_prefix`
   must be `repo:<owner>@<owner_id>/<repo>@<repository_id>` with the observed
   owner and repository IDs. The inputs default to
   `github_subject_format = "immutable"`. A repository that still uses the old
   subject shape must select `github_subject_format = "default"`. Do not change
   GitHub customization or admit both formats to fit a stale policy.
2. **Run the foundation bootstrap.** `deploy.py gcp-foundation` checks the project
   number and the repository IDs, compares the saved plan with the trust that the
   inputs authorize, and stops before the apply on any difference. For build
   contexts it then writes the `gcp-image-build` and `gcp-image-validate`
   Environments with their protected branches, secrets, and variables.
3. **Read back.** Confirm the exact `attributeCondition` with `gcloud iam
   workload-identity-pools providers describe`, and confirm with each identity's
   `get-iam-policy` that it holds only its purpose subject. Run one protected
   build and confirm that a feature ref fails before cloud authentication.

The `check-tf-gcp-wif-trust` guard reconciles the provider union with the
purpose map, rejects overlapping subjects and broad validate or deploy/destroy
image and evidence grants, rejects project-wide release-scan grants, and
inventories the purpose callers. Deploy and destroy compute access uses the
network and security administration roles; their storage custom roles are
condition-bound to the deterministic platform assets, audit-log, and GDC-image
buckets rather than granted project-wide Storage Admin. The state bucket is
included automatically. Any non-empty `raes_package_bucket_name` or
`ctf_content_bucket_name` must also be passed to the foundational identity root
through `platform_external_bucket_names`; it grants bucket-level IAM
administration to each exact resource so platform-core can maintain its
workload bindings.

## GCP guest image builds (developer build project)

A developer builds, validates, and publishes the guest images in a build project
that they supply (#2668, ADR-070 decision 8). `deploy.py gcp-foundation` with the
build-only inputs (`scripts/bootstrap/gcp-image-builder.example.tfvars.json`)
creates the project resources and writes the two Environments of the developer
repository. Do not set these values by hand unless the bootstrap can not reach
GitHub.

Consumed by `.github/workflows/packer-gcp.yml` (build and publish) and
`.github/workflows/packer-gcp-validate.yml` (validation, called by the build).
The jobs run on `ubuntu-latest` with Workload Identity Federation and no
long-lived service-account key. Variables reach Packer as `PKR_VAR_*`
environment values, never `-var` CLI flags.

| Name | Type | Environment | Purpose |
|------|------|-------------|---------|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | secret | `gcp-image-build`, `gcp-image-validate` | Foundation output `workload_identity_provider`. |
| `GCP_PROJECT_ID` | secret | `gcp-image-build`, `gcp-image-validate` | The build project. |
| `GCP_PACKER_BUILD_SERVICE_ACCOUNT` | secret | `gcp-image-build` | WIF caller and the only identity of the Packer VM, the Cloud Build export, and the export worker. It alone creates the immutable `packer-builds/<numeric-image-id>/build-evidence.json` records. |
| `GCP_PACKER_VALIDATE_SERVICE_ACCOUNT` | secret | `gcp-image-validate` | WIF caller for the no-SA validation VM, the credentialless SBOM scanner, the read-only candidate disk, and the evidence labels. It reads only build records and creates only validation records. |
| `GCP_REGION` | variable | both | Build region. Default `us-central1`. |
| `GCP_PACKER_NETWORK` | variable | both | Foundation output `image_build_network_name`. |
| `GCP_PACKER_SUBNETWORK` | variable | both | Foundation output `image_build_subnetwork_name`. |
| `GCP_PACKER_USE_INTERNAL_IP` | variable | `gcp-image-build` | `true`: builds have no external IP and reach the builder over IAP. |
| `GCP_GCE_BASE_IMAGE_BUCKET` | variable | `gcp-image-build` | Foundation output `image_export_bucket_name`: the staging bucket of the disk export. |
| `GCP_PACKER_ZONE` | variable | optional | Build zone. Default `${GCP_REGION}-a`. |
| `GCP_PACKER_MACHINE_TYPE` | variable | optional | Builder machine type. Default `e2-standard-2`. |
| `GCP_VALIDATE_MACHINE_TYPE` | variable | optional | Validation VM machine type. Default `e2-standard-4`. |

The publish job exports and pushes an image only after the validation job of the
same run passes for the exact numeric image ID. For Windows-family builds, the
workflow generates a throwaway `winrm_bootstrap_password` for each run, and the
pre-promoted `dc-prebaked` build also generates a DSRM password
(`PKR_VAR_dc_dsrm_password`). Nothing is committed.

Validation first reads the immutable build record at the exact numeric image ID
and rejects any mismatch in repository, protected ref, source revision, name, ID,
family, image type, or Environment. It collects the guest SBOM on a separate
credentialless Ubuntu scanner from a candidate-derived disk attached read-only and
mounted `ro,nosuid,nodev,noexec`; the candidate never runs the scanner or writes
its output. Raw validation evidence and the SBOM stay in the private evidence
bucket; only a redacted verdict is uploaded.

## AWS portal (`dev` / `prod`)

Consumed by `.github/workflows/_shifter-platform.yml`. The committed
`platform/terraform/environments/<env>/portal/terraform.tfvars` is an
`example.com` baseline. The workflow's `Render local.auto.tfvars from
deployment secret` step, present in both the `plan` and `apply` jobs,
writes the real per-deployment values into a gitignored
`local.auto.tfvars` before Terraform runs, so deploys never plan or apply
against the baseline. The step picks the secret by environment and fails
loud (`::error::`) when the active environment's secret is empty.

| Name | Kind | Required | Notes |
|---|---|---|---|
| `AWS_ROLE_ARN_DEV` / `AWS_ROLE_ARN` | secret | yes | OIDC role assumed by the deploy job. |
| `TF_VARS_DEV_PORTAL` | secret | yes (dev) | Whole-file `local.auto.tfvars` payload for the dev portal root, rendered verbatim over the committed baseline before `terraform plan` / `apply`. |
| `TF_VARS_PROD_PORTAL` | secret | yes (prod) | As above, for the prod portal root. |

The `TF_VARS_<ENV>_PORTAL` secret holds plain Terraform HCL, the same
content you would put in a `local.auto.tfvars`. At minimum it must set the
values the `example.com` baseline deliberately leaves non-operational:

- `domain_name`, `ses_domain`, `ctfd_domain`, `ctf_from_email`
- `alarm_email`
- `allowed_email_domains` (deliberately empty in the baseline, fails
  closed)
- `ctfd_ssh_public_key`, `ctfd_ssh_allowed_cidrs` (empty in baseline,
  CTFd SSH ingress closed)
- `user_storage_bucket` and any other AWS-account-suffixed bucket names
  that vary per environment

For native scenario-provided CTF challenges, the same overlay may also set
`ctf_content_bucket_arn`, `ctf_content_prefix`, and
`ctf_content_max_bytes`. The bounded reference catalog belongs in the existing
application secret, not in the tfvars payload. See the
[native CTF scenario-content runbook](ctf-scenario-content.md).

For AWS local deploys, write the same HCL to a gitignored
`local.auto.tfvars` next to the environment's `terraform.tfvars` and run
`terraform apply` from a workstation that has the target role (see
**Local development** below).

## AWS core (`dev` / `prod`)

Consumed by `.github/workflows/_core.yml`. The committed
`platform/terraform/environments/<env>/main.tf` defines shared ECR repos and
S3 budget alerts. Budget notification recipients are deployment-owned: the
workflow's `Render local.auto.tfvars from deployment secret` step writes
`budget_alert_email` into a gitignored `local.auto.tfvars` before Terraform
runs. The step picks the secret by environment and fails loud (`::error::`) when
the active environment's secret is empty.

| Name | Kind | Required | Notes |
|---|---|---|---|
| `TF_VARS_DEV_CORE` | secret | yes (dev) | Whole-file `local.auto.tfvars` payload for the dev core root. Must set `budget_alert_email` to a real operations address. |
| `TF_VARS_PROD_CORE` | secret | yes (prod) | As above, for the prod core root. |

See `platform/terraform/environments/<env>/local.auto.tfvars.example` for the
minimum HCL shape.

### Fresh AWS account bootstrap order

Standup runbooks for the phases referenced below:

- Runner provisioning: [`aws-runner-provisioning-runbook.md`](aws-runner-provisioning-runbook.md)
- Range AMI seeding: [`aws-ami-seeding-runbook.md`](aws-ami-seeding-runbook.md)
- Terraform apply order: [`aws-terraform-apply-order.md`](aws-terraform-apply-order.md)
- Tearing an environment down: [`aws-teardown-runbook.md`](aws-teardown-runbook.md)

For a new AWS account, bootstrap the backend and CI identity before trying
run an AWS dev deploy (`gh workflow run deploy.yml --ref <branch> -f environment=aws-dev`):

If the account was **re-used** (a prior BigRAE tenant was torn down here), first run
`./scripts/bootstrap/deploy.py account-recovery --env dev --profile <profile>` to detect
orphaned control-plane leftovers that would otherwise fail `terraform apply` one
collision at a time, and add `--sweep` to delete the owned ones. It refuses on a live
tenant and never touches data-bearing resources. See `scripts/bootstrap/README.md` and
`aws-teardown-runbook.md`. Off a TTY (CI or an automation wrapper), pass `--yes` on the
bootstrap commands below so their confirmation prompts proceed without a terminal;
`--yes` does not authorize the recovery sweep, which always needs `--sweep`.

1. Run `./scripts/bootstrap/deploy.py bootstrap --env dev --profile <profile>`.
   This creates the shared S3 state bucket, creates the GitHub OIDC role,
   sets `AWS_ROLE_ARN_DEV` and `TF_INFRA_STATE_BUCKET_DEV` (the env-suffixed
   names the dev deploy workflows read), and writes per-instance Terraform
   backend configs under `~/.shifter/<env>-<bucket>/`.
2. Run `./scripts/bootstrap/deploy.py runners --env dev --profile <profile>` to
   provision **and** auto-register the self-hosted runners (issue #1433). It
   provisions a dedicated, ADR-004-R20-compliant runner VPC by default, applies
   the runner root, and registers each runner over SSM (per-runner single-use
   token, never persisted). The dedicated runner VPC is the standard placement
   (ADR-004-R20, #1437), selected by the tracked runner tfvars. Pass
   `--use-existing-network` only to place runners in an existing compliant
   network, such as the portal VPC private tier, whose `vpc_id`/`subnet_id` you
   keep in a gitignored `local.auto.tfvars`. Never commit live VPC/subnet IDs.
   The account default VPC isn't a supported placement: `allow_default_vpc` is a
   documented exception only. AWS deploy workflows use `runs-on: self-hosted`.
3. Ensure `/shifter/ami/{kali,ubuntu,windows,dc}` exists in SSM Parameter
   Store before portal Terraform plans/applies. The Packer workflow updates
   these parameters after AMI builds; in a moved account, set the builder-network
   repository variables `PACKER_BUILD_{VPC,SUBNET}_ID_<ENV>` (and the
   `PACKER_VERIFY_*` fresh-boot gate variables) first, since the committed
   `dev.pkrvars.hcl` ships only placeholders. The Kali build also requires
   the target account to accept the free AWS Marketplace terms for product
   code `7lgvy7mt78lgoi4lant0znp5h`. See the
   [AWS AMI seeding runbook](aws-ami-seeding-runbook.md).
4. Review `TF_VARS_DEV_PORTAL` for account-specific values such as domain
   names, alarm email, SSH allowlists, and bucket names. Review
   `TF_VARS_DEV_RANGE` for range deployment values such as the agent S3 bucket
   and the regional PAN-OS VM-Series AMI, and set `SHIFTER_CONFIG_DEV_RANGE` to
   the deployment's `bigrae.yaml` (its `settings.range_egress` is rendered into
   the range egress allowlist). Bootstrap configures the AWS role secret and
   backend files; the deploy workflows fail loud when the active portal or range
   tfvars secret, or the range `bigrae.yaml`, is missing.
5. For the first deploy in a moved or fresh account, run the `Deploy` workflow
   with `gh workflow run deploy.yml --ref <branch> -f environment=aws-dev`
   (add `-f aws_first_deploy=true` for the first-ever engine deploy, before the
   platform Terraform apply has created the provisioner task definition). The
   dispatch forces the full AWS chain (Core -> Range -> Engine -> Platform).
   Deployment is always a manual dispatch; pushing to a branch runs validation
   only.

### First-run DNS validation

The first platform apply in a fresh account creates DNS-validated ACM
certificates for the portal domains and SES identities for the configured
mail domain. Publish these records in the authoritative DNS zone while the
apply is running; Terraform will continue once AWS observes the ACM records.

- ACM portal and Guacamole URL validation records are exposed by the root
  Terraform output `acm_validation_records`. They are CNAME records.
- SES domain verification uses a TXT record named `_amazonses.<ses_domain>`.
  Fetch the value with:
  `aws ses get-identity-verification-attributes --identities <ses_domain> --region <region>`.
- SES DKIM uses three CNAME records. Fetch the tokens with:
  `aws ses get-identity-dkim-attributes --identities <ses_domain> --region <region>`.
  Each token maps `<token>._domainkey.<ses_domain>` to
  `<token>.dkim.amazonses.com`.
- In Cloudflare, create ACM and DKIM CNAMEs as DNS-only records. Do not proxy
  validation CNAMEs.

### First-run DNS routing

After the first platform apply creates the portal ALB and CTFd instance,
publish the runtime DNS records:

- `domain_name` and `chat.<domain_name>` point to the Terraform output
  `alb_dns_name` as CNAME records. In Cloudflare, keep them DNS-only unless
  the deployment explicitly validates proxied Cloudflare behavior.
- `ctfd_domain` points to the Terraform output `ctfd_elastic_ip` as an A
  record. In Cloudflare, publish the record as DNS-only until the CTFd host
  has completed certbot and `https://<ctfd_domain>/login` works against the
  origin. After that validation, the CTFd `A` record may be switched to
  proxied with SSL/TLS mode `Full (strict)`. The DNS target is the bare
  Elastic IP address, never an `http://` or `https://` URL. Cloudflare
  challenge actions must be disabled or explicitly skipped for the CTFd
  hostname before event smoke testing; the CTFd sync scripts and automated
  checks expect the CTFd app to answer directly instead of a Cloudflare
  `cf-mitigated: challenge` page.

ALB access logs use a dedicated S3 bucket with SSE-S3 because Elastic Load
Balancing does not support SSE-KMS for Application Load Balancer access-log
delivery. Central Firehose log archives continue to use the log-aggregation
customer-managed KMS key.

### First-run private AWS API reachability

The portal VPC creates private AWS service endpoints for the runtime services
that must be reachable before application containers can start: ECR, S3,
CloudWatch Logs, Secrets Manager, SSM, STS/KMS, ECS/EC2/ELB, SNS, SQS, and
DynamoDB. These endpoints are part of the expected fresh-account platform
shape. They keep EC2 user_data, Docker image pulls, ECS secret resolution, and
awslogs setup from depending on the portal inspection firewall/NAT egress path
during first deploy. The portal EC2 user_data also retries the AL2023 package
metadata/install step so a transient repository timeout does not permanently
strand cloud-init.

Range status propagation depends on the portal worker containers consuming the
encrypted portal messaging SQS queues. If range provisioner tasks exit 0 but
CMS or CTF rows remain `pending`, check `docker logs worker-cms` on the portal
EC2 instance. `KMS.AccessDeniedException` on the portal messaging CMK means the
portal EC2 role is missing the SQS KMS decrypt/data-key grant; the
retained portal services must receive `module.messaging.kms_key_arn` and attach its
`sqs-kms-access` policy.

When portal inspection is enabled, ALB health checks and user traffic route
through the inspection boundary before reaching the private portal and
Guacamole targets. The targets therefore allow ingress both from the ALB
security group and from the portal public subnet CIDRs; the CIDR rule is still
VPC-local and is required for the routed middlebox path.

`enable_portal_inspection` defaults to `false` in the committed
`platform/terraform/environments/<env>/portal/terraform.tfvars` (#932). A deploy
that wants inspection sets `enable_portal_inspection = true` in its
`TF_VARS_<ENV>_PORTAL` secret payload (rendered as `local.auto.tfvars`, which
overrides the committed baseline). Enabling it removes the direct private → NAT
default route, so the portal apply job runs
`scripts/assert_portal_inspection/assert_portal_inspection.py` after
`terraform apply`: it fails the deploy if the live route tables do not point at
healthy per-AZ Network Firewall endpoints, instead of silently blackholing
egress. The assertion is a no-op when inspection is off.

### Post-deploy smoke secrets (dev)

The dev `post-deploy-smoke` job in `.github/workflows/_shifter-platform.yml`
runs `scripts/smoke-test.sh`, which provisions a minimal range as a smoke user,
verifies guest connectivity, and tears it down. It runs only for dev applies and
is `continue-on-error`, so a smoke failure does not fail the deploy; instead the
job opens a `[smoke-test]` bug issue (labels `bug`, `smoke-test`).

The smoke validates the **platform**, not scenario content: its ranges are built
entirely from base range AMIs (`smoke_linux` = Kali + Ubuntu, `smoke_windows` =
Kali + plain Windows, all `xdr_agent: false`), so it requires **no XDR agent and
no `SMOKE_*_AGENT_ID` secrets** (#1422). XDR/agent install is scenario content
and is exercised by real scenarios, not the smoke. Only one secret is needed,
set manually (not written by bootstrap or `sync-deploy-secrets.sh`):

| Name | Kind | Required | Notes |
|---|---|---|---|
| `SMOKE_TEST_USER_EMAIL` | secret | yes (dev smoke) | Email of the portal user the smoke provisions as. If the user does not exist yet it is created on first run. When unset, the smoke step fails loud before doing any work. |

## AWS range (`dev` / `prod`)

Consumed by `.github/workflows/_range.yml`. The committed
`platform/terraform/environments/<env>/range/terraform.tfvars` is an
account-neutral baseline. The workflow's `Render local.auto.tfvars from
deployment secret` step is present in both the `plan` and `apply` jobs; it
selects the active environment strictly from `inputs.is_dev`, writes the
matching whole-file secret into `local.auto.tfvars`, and fails loud
(`::error::`) when the active secret is empty.

| Name | Kind | Required | Notes |
|---|---|---|---|
| `TF_VARS_DEV_RANGE` | secret | yes (dev) | Whole-file `local.auto.tfvars` payload for the dev range root, rendered verbatim over the committed baseline before `terraform plan` / `apply`. Carries only the non-egress overrides below, **not** `victim_allowed_cidrs`. |
| `TF_VARS_PROD_RANGE` | secret | yes (prod) | As above, for the prod range root. |
| `SHIFTER_CONFIG_DEV_RANGE` | secret | yes (dev) | Full deployment `bigrae.yaml` (root installation config) for dev. The workflow renders its `settings.range_egress` into `victim_allowed_cidrs.auto.tfvars` before `terraform plan` / `apply`, so the egress allowlist is sourced once from `bigrae.yaml` (#1015). |
| `SHIFTER_CONFIG_PROD_RANGE` | secret | yes (prod) | As above, for the prod range. |

Dev and prod range secrets are covered by the sync script.

At minimum, each `TF_VARS_<ENV>_RANGE` payload must set the deployment-specific
values stripped from the committed baseline:

- `agent_s3_bucket`: the account-specific user-storage bucket read by range
  instance roles
- `vm_series_ami_id`: the regional PAN-OS Marketplace AMI to use. This is
  deployment configuration, not a credential; keep it in the overlay so the
  shared repo does not prescribe a marketplace version/region for every
  deployment.

The range egress allowlist (`victim_allowed_cidrs`) is **not** part of
`TF_VARS_<ENV>_RANGE`. The workflow's `Render range egress allowlist from
bigrae.yaml` step (in both the `plan` and `apply` jobs) writes
`SHIFTER_CONFIG_<ENV>_RANGE` to an ephemeral file and runs `bigrae-config
render` to generate `victim_allowed_cidrs.auto.tfvars`, so the configured policy
and the deployed firewall cannot drift (#958 / #1015). Putting
`victim_allowed_cidrs` in `TF_VARS_<ENV>_RANGE` re-creates that drift; keep it
out of the whole-file secret.

Local operators use the same model: write those values to a gitignored
`local.auto.tfvars` alongside the range `terraform.tfvars`. In this repo's
worktree workflow, `scripts/setup-worktree.sh` symlinks existing
`local.auto.tfvars` overlays from the main checkout into worktrees so local
plans do not lose the per-account values when branches change.

The committed `terraform.tfvars` also ships an empty `victim_allowed_cidrs`
baseline so the repo never carries a deployment's allowlist (PLAT-220 / #775).
This completes the `_core.yml` / `_range.yml` deploy-tfvars audit from #784:
the AWS core tfvars remain generic repository-name defaults, while account-bound
and deployment-specific range values are supplied by secret or local overlay.

For the PLAT-220 range egress allowlist:

| File                                                                                  | Status                                                |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `platform/terraform/environments/{dev,prod}/range/terraform.tfvars`                  | committed; empty `victim_allowed_cidrs` baseline      |
| `platform/terraform/environments/{dev,prod}/range/local.auto.tfvars.example`         | committed; shape reference                            |
| `platform/terraform/environments/{dev,prod}/range/local.auto.tfvars`                 | gitignored; operator's non-egress account overrides (NOT `victim_allowed_cidrs`) |
| `platform/terraform/environments/{dev,prod}/range/victim_allowed_cidrs.auto.tfvars`  | gitignored; rendered by `bigrae-config render` (CI: from `SHIFTER_CONFIG_<ENV>_RANGE`; local: from your `bigrae.yaml`) |

Source for the PANW Cortex XSIAM/XDR allowlist:
<https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Administrator-Guide/Resources-Required-to-Enable-Access>.

See `docs/architecture/range-egress-ip-allowlist.md` for the full mapping
between `bigrae.yaml.settings.range_egress` and the Terraform inputs.

## GCP range (`gcp-dev`)

The GCP range network egress allowlist is rendered from the deployment's
`bigrae.yaml`. `gcp-deploy` writes it into its private `settings.auto.tfvars`,
separate from the operator's Terraform values. Two Terraform variables expose
the platform contract:

| Variable                       | Type           | Meaning                                                                          |
| ------------------------------ | -------------- | -------------------------------------------------------------------------------- |
| `range_egress_mode`            | `string`       | One of `status-quo` (default), `deny-all`, `allowlist`                           |
| `range_egress_allowed_cidrs`   | `list(string)` | CIDR allowlist when `range_egress_mode = "allowlist"`                            |

The committed `terraform.tfvars` baseline sets `range_egress_mode =
"status-quo"`. Deployments that want enforcement set
`settings.range_egress` in `bigrae.yaml`; the deploy renders it into the
private var file that loads after the operator's values.

| Name | Kind | Required | Notes |
|---|---|---|---|
| `SHIFTER_CONFIG_GCP_DEV` | secret | yes | Full deployment `bigrae.yaml` (root installation config) for a CI deployment. The CI adapter writes it as the bundle's `bigrae.yaml`, and `gcp-deploy` renders the egress variables from it before `terraform apply`. The render fails loud when the secret is empty. |

- **CI (`_gcp-dev.yml`)** renders the bundle from `SHIFTER_CONFIG_GCP_DEV`;
  `gcp-deploy` renders the egress variables.
- **A deployment without GitHub** keeps `bigrae.yaml` in its config bucket;
  `gcp-deploy` renders the same variables.
- **Local `terraform` runs** use the operator workflow in
  `docs/architecture/range-egress-ip-allowlist.md` (run `bigrae-config render`
  by hand, then `terraform apply`).

`bigrae.yaml` holds secret *references*, not values, but it is the
deployment's authoritative config; carry it as a GitHub secret so CI does not
read a second, drift-prone copy of the allowlist. Do **not** also set
`range_egress_mode` / `range_egress_allowed_cidrs` in the Terraform values;
that re-introduces the drift this flow removes.

### GCE range-cell backend variables

The GCP range backend is `gce` (GCE range cells). A deployment without GitHub
sets these non-secret inputs in `bigrae.yaml` (`settings.range_cells` and
`settings.participant_openvpn`) and in `range-images.json`. A CI deployment
sets them as GitHub Actions repository or environment **variables** (`vars.*`);
the CI adapter projects each one onto the same setting. `gcp-deploy` passes them
to the generated runtime config. Unset values fall back to the code defaults in
`config.py`. See `docs/dev/gcp-range-cell-deploy.md` for the operator runbook.

`./scripts/bootstrap/deploy.py gcp-images --project-id <project> --store
gs://<bucket>` verifies the Kali, Ubuntu, and DC images that the release
selects, imports or reuses them, and writes their exact references to
`range-images.json`. Without `--store`, it prints the references; a CI
deployment sets the `GCP_RANGE_*_IMAGE` variables to them. The command needs no
`GCP_GCE_BASE_IMAGE_BUCKET`. Set these values by hand only to pin a specific
image outside that flow.

| Variable | Required | Notes |
|---|---|---|
| `GCP_RANGE_BACKEND` | no | `gce` (default), the supported native GCE range-cell backend. The legacy `gdc` VM Runtime backend is retired and removed root-and-branch in #2311. |
| `GCP_RANGE_CELL_PROJECT_ID` | no | Project the range cells provision into. Defaults to the project parsed from the range VPC self-link (`range_network_id`), so it is correct even while the control-plane `GCP_PROJECT_ID` is a deploy-overlay placeholder. |
| `RANGE_NETWORK_ZONE` | yes | Compute Engine zone for range guests, for example `us-central1-a`. |
| `GCP_RANGE_LINUX_IMAGE` | yes | Full image or family URL for the default unkeyed Linux/host profile. |
| `GCP_RANGE_DC_IMAGE` | yes | Default unkeyed Windows domain-controller image, pre-promoted at bake time. Baked per-domain from `dc-prebaked.pkr.hcl` into family `shifter-<purpose>-dc` (see "Baking a new pre-promoted DC image" in `docs/dev/gcp-range-cell-deploy.md`). |
| `DC_DOMAIN_PASSWORD` | DC scenarios | **Sensitive.** Domain Administrator password the provisioner sets on the pre-promoted DC (`set_admin_password`). Must match the password baked into the DC image by `a2_setup.ps1` at bake time (its `-AdminPassword` default). Provide via the GCP deploy config secret so it is rendered into the runtime env and forwarded to the provisioner job (`_GCP_PROVISIONER_ENV_KEYS`); if unset, DC setup fails setting the admin password. |
| `GCP_RANGE_KALI_IMAGE` | scenario | Default unkeyed Kali image. Keyed guests use the structured map below. |
| `GCP_RANGE_WINDOWS_IMAGE` | scenario | Generic Windows guest image. |
| `GCP_RANGE_IMAGE_KEY_PROFILES_JSON` | keyed scenarios | Optional compact JSON map from exact `(linux|kali|windows|dc, ami_key)` to a complete GCE profile. Normal images use `source_image`, sizing, disk policy, and a typed capability. Preconfigured hosts use an exact `source_machine_image`, machine type, host login, participant container/account, the closed `participant-readiness/v1` contract, and a lowercase SHA-256 readiness-manifest digest. Any profile may opt into public TCP 80/443 with `allow_public_web_egress` (default false). Maximum 32,768 bytes and 64 entries. Unknown keys, unsupported capabilities, and malformed profiles fail before cloud mutation. Use an Actions environment secret when resource names or logical selectors are confidential; the deploy workflow prefers that secret over the repository variable. The value is runtime configuration, not a credential, and is emitted into the private platform ConfigMap. See `docs/dev/gcp-range-cell-deploy.md`. |
| `GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL` | not read | Keyless range host identity attached to range guests for default-on model access (ADR-064): a predict-only Vertex role plus host telemetry, delivered via Workload Identity with no key material. The deploy takes it from the Terraform output `range_host_service_account_email`; set no variable. When the model broker is the guest path (`MODEL_BROKER_GUEST_VIP` set) guests stay identity-less instead. |
| `GCP_RANGE_HOST_IDENTITY_POOL_SIZE` | machine-image hosts | Number of Terraform-created `sh-range-host-<slot>` identities. Must equal `range_host_identity_pool_size`; zero disables preconfigured machine-image hosts. |
| `GCP_RANGE_PRIVATE_GOOGLE_ACCESS` | no | Set `true` so no-external-IP guests reach approved Google APIs (including Vertex for default-on model access) over Private Google Access. Leave it `false` when the model broker is enabled: broker-enrolled ranges refuse to provision with Private Google Access on. |
| `RANGE_OPENVPN_ENABLED` | no | Set `true` to give participants an OpenVPN profile for their range's single participant-access target (ADR-039-R10). Projected onto `settings.participant_openvpn.enabled`. It also deploys the shared OpenVPN server pool (#2480): Terraform `openvpn_pool_enabled` follows this setting, and `gcp-deploy` creates the pool PKI and release record. Apply `global/cicd-oidc` first so the deploy identity holds the conditioned pool role. Defaults to `false`. See "Participant OpenVPN access" in `docs/dev/gcp-range-cell-deploy.md`. |

By default (ADR-064) range guests hold a keyless, predict-only Vertex identity via
Workload Identity so they can reach models directly; enabling a specific model in
the provider console is the only manual step. When the ADR-059 broker is enabled
it becomes the guest model path and guests stay identity-less, with an admitted
allocation and trusted one-use enrollment. The legacy invocation service account,
its provisioner key-admin grants and per-range keys stay removed on Terraform
apply; revoke externally managed shared keys through their owning deployment
before declaring migration complete.

## Local development

For local `terraform plan` / `terraform apply` against your own cloud
account:

```sh
# AWS dev portal
cat > platform/terraform/environments/dev/portal/local.auto.tfvars <<EOF
domain_name           = "shifter.your-domain.example"
ses_domain            = "your-domain.example"
ctfd_domain           = "ctf.shifter.your-domain.example"
alarm_email           = "your-team-alerts@your-domain.example"
allowed_email_domains = ["your-domain.example"]
user_storage_bucket   = "shifter-dev-user-storage-<your-account-id>"
EOF

cd platform/terraform/environments/dev/portal
terraform init -backend-config=dev.s3.tfbackend
terraform plan
```

```sh
# AWS dev range
cat > platform/terraform/environments/dev/range/local.auto.tfvars <<EOF
agent_s3_bucket = "shifter-dev-user-storage-<your-account-id>"
vm_series_ami_id = "ami-xxxxxxxxxxxxxxxxx"
victim_allowed_cidrs = [
  "<your-required-egress-cidr>/32",
]
EOF

cd platform/terraform/environments/dev/range
terraform init -backend-config=dev.s3.tfbackend
terraform plan
```

For event-sized AWS dev runs, put the full capacity overlay in
`TF_VARS_DEV_PORTAL`. The deploy workflow reads `terraform output -json` from
the applied portal state and derives its single-instance vs. ASG path from the
Terraform `enable_autoscaling` output; there is no separate GitHub variable to
keep in sync. The committed `terraform.tfvars` is a modest OSS default, while
the secret payload below is the event-sized overlay for ASG, warm pool, Redis
channel layer, and larger Guacamole/Postgres capacity:

```hcl
enable_autoscaling     = true
asg_min_size           = 2
asg_max_size           = 8
asg_desired_capacity   = 2
asg_warm_pool_min_size = 2
asg_warm_pool_state    = "Stopped"
enable_redis           = true

db_instance_class        = "db.m6i.xlarge"
db_allocated_storage     = 100
db_max_allocated_storage = 500
db_backup_retention_days = 7

redis_node_type = "cache.m6g.xlarge"

guacd_cpu                      = 2048
guacd_memory                   = 4096
guacamole_client_cpu           = 2048
guacamole_client_memory        = 4096
guacd_desired_count            = 6
guacamole_client_desired_count = 4

guacamole_db_instance_class        = "db.m6i.xlarge"
guacamole_db_allocated_storage     = 100
guacamole_db_max_allocated_storage = 500
guacamole_db_multi_az              = true

guacamole_enable_autoscaling       = true
guacamole_autoscaling_min_capacity = 4
guacamole_autoscaling_max_capacity = 10
guacamole_autoscaling_cpu_target   = 60
```

> **Keep the dev overlay minimal unless an event needs it.**
> `TF_VARS_DEV_PORTAL` is the *only* place the running dev fleet size is set:
> the committed `terraform.tfvars` is a fail-loud baseline that this secret
> always overrides. Nothing reconciles it automatically; edit your local
> overlay and push it with `scripts/sync-deploy-secrets.sh` (see [Populating
> and syncing the secrets](#populating-and-syncing-the-secrets)). The overlay
> above is *event-sized*. Left in place between events it makes every
> deploy run a full multi-instance ASG instance refresh (desired + warm pool ≈
> a dozen instances, roughly an hour) and holds prod-class RDS/Redis/Guacamole
> capacity. For ordinary dev, keep the ASG at a single instance with a minimal
> warm pool and shrink the DB/Redis/Guacamole lines to match:
>
> ```hcl
> asg_min_size           = 1
> asg_max_size           = 2
> asg_desired_capacity   = 1
> asg_warm_pool_min_size = 1
> ```
>
> Apply the event-sized overlay only for the duration of an event, then revert.

```sh
# GCP: keep the values in an operator-owned directory outside the repository
values_dir="<operator values directory>"
cat > "$values_dir/local.auto.tfvars" <<EOF
project_id                    = "your-gcp-project-id"
dynamic_secret_project_id     = "your-gcp-range-secret-project-id"
environment                   = "<deployment>"
region                        = "us-central1"
artifact_registry_location    = "us-central1"
public_hostname               = "shifter.your-domain.example"
enable_managed_tls            = true
identity_allowed_email_domain = "your-domain.example"
gke_subnet_cidr               = "10.40.0.0/20"
gke_pods_cidr                 = "10.44.0.0/16"
gke_services_cidr             = "10.45.0.0/20"
gke_master_ipv4_cidr          = "172.16.0.0/28"
gke_master_authorized_cidrs   = []
range_network_cidr            = "10.50.0.0/16"
EOF

export TF_DATA_DIR="$values_dir/.terraform"
root=platform/terraform/gcp/environments/platform
terraform -chdir="$root" init -reconfigure \
  -backend-config="bucket=<existing-state-bucket>" \
  -backend-config="prefix=shifter/<deployment>/platform-core"
terraform -chdir="$root" plan -var-file="$values_dir/local.auto.tfvars"
```

The other platform variables have defaults in
`platform/terraform/gcp/environments/platform/variables.tf`. Never commit a
`local.auto.tfvars`. A full GCP deploy uses
`gcp-deploy` instead (see the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md)); it writes
its var files and its `TF_DATA_DIR` into a private temporary directory.
