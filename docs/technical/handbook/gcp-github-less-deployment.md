# GitHub-less GCP deployment

Applicability: GCP deployments that use the GitHub-less deploy path (ADR-070).
Operator: platform owner. Verified against the repository: 2026-10-29.

This guide stands up, updates, and tears down a GCP deployment from a
published release. The deployment needs no GitHub access, no GitHub
Environment, no self-hosted runner, and no repository checkout. The commands
are in `scripts/bootstrap/deploy.py`, and the deployer image of each release
holds them. The design is in
`docs/architecture/github-less-gcp-deployment-design-2571.md`.

An operator installs with one bundle and one command, `gcp-install`
([Install](#install)), and updates with `gcp-deploy`
([Update and roll back](#update-and-roll-back)).

A CI deployment (`deploy.yml` with `_gcp-dev.yml`) runs the same commands. Its
GitHub inputs are in [deploy-secrets.md](../../dev/deploy-secrets.md#gcp-gcp-dev).
When a command fails, use [RB023](../../runbooks/rb023-gcp-deployment.md).

## Terms

| Term | Meaning |
| --- | --- |
| Release | One source revision: five control-plane images, one deployer image, and their attestations in the release registry. |
| Release registry | The public registry `ghcr.io/<owner>/<repository>` that `release-gcp.yml` writes and every deployment reads. |
| Mirror | A registry that an operator fills with the release images by digest, for a deployment that cannot reach the release registry. |
| Release manifest | `bigrae.gcp-release.v1` JSON with the source revision, the chart version, the exact image digests, the pinned base range images, and the authorization model. The deployer image holds it at `/opt/bigrae/release.json`. |
| Deployer image | The container that holds the commands, the Terraform roots, the Helm chart, and the pinned tools of one release. |
| Deployment input bundle | The objects that one deployment reads: `bigrae.yaml`, the Terraform values, and the range image references. |
| Deployment config bucket | The GCS bucket that holds the deployment input bundle and the deployment lock. |
| Purpose identity | One of the deploy, release-scan, and destroy service accounts of a deployment. |

## Prerequisites and roles

1. Make sure that the GCP project exists and that billing is on.
2. Enable every Vertex AI model that the deployment configuration uses. The
   Vertex AI API alone does not enable a model.
3. Make sure that your Google identity can administer the project IAM:
   `roles/owner`, or `roles/resourcemanager.projectIamAdmin`,
   `roles/iam.roleAdmin`, and `roles/iam.serviceAccountAdmin`. `gcp-install`
   applies the foundation and names your identity in
   `impersonation_principals` for `deploy`, `release_scan`, and `destroy`. The
   foundation gives each listed principal
   `roles/iam.serviceAccountTokenCreator` on that purpose identity only.
4. Make sure that you can pull from the release registry. It is public. A
   deployment that cannot reach it uses a mirror
   ([Deploy from a mirror](#deploy-from-a-mirror)).
5. Sign in to Google Cloud. The commands use your credentials only to
   impersonate the purpose identities.

   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```

6. Use the deployer image of the release
   ([Get and verify the deployer image](#get-and-verify-the-deployer-image)).
   The image holds every tool at the release version.
7. If you run the commands from a checkout instead, check out the exact release
   revision with no local changes. Install these tools on `PATH`: `gcloud`,
   `gke-gcloud-auth-plugin`, `terraform`, `kubectl`, `helm`, `oras`, `trivy`,
   `git`, and `uv`. The command stops when a tool is missing.
8. From a checkout, run each command through the `scripts/bootstrap` uv
   project. The project holds the Python dependencies of the commands. In this
   guide, `./scripts/bootstrap/deploy.py` means:

   ```bash
   uv run --project scripts/bootstrap scripts/bootstrap/deploy.py <command>
   ```

The foundation needs project administration rights. `gcp-images` runs as your
own identity: it creates Compute Engine images and a temporary transfer bucket
in the project.

## Publish a release

The repository maintainer does this procedure once for each release. A
deployment operator only consumes the result. One release serves every
deployment, and a new deployment needs no change here.

1. Create the `gcp-release` GitHub Environment once. Limit it to the release
   refs `refs/heads/main`, `refs/heads/dev`, and `refs/heads/deploy-test`.
   Protect each of these branches. The Environment holds no secrets or
   variables.
2. Merge the Release Please release PR on `main`. The `release-gcp` job of
   `release-please.yml` then builds the release for the tagged commit (#2660).
   To build a test release from `dev` or `deploy-test`, or to publish a release
   again, dispatch the workflow. The `version` input is optional; an empty value
   uses `version.txt`.

   ```bash
   gh workflow run release-gcp.yml --ref dev
   ```

3. Read the deployer image digest and the release manifest in the run summary.
4. After the first release only, make each package public in the GitHub
   package settings: `portal`, `provisioner`, `guacd`, `guacamole-client`,
   `openvpn`, and `deployer-gcp`. GHCR makes a new package private.

The workflow publishes to `ghcr.io/<owner>/<repository>`, in lower case, with
the workflow `GITHUB_TOKEN`. It uses no cloud identity.

The workflow scans the deployer image digest before it attests the image. The
scan is a blocking Trivy gate: `trivy image --scanners vuln --severity
HIGH,CRITICAL --ignore-unfixed --exit-code 1`. Trivy reads its vulnerability
databases from `mirror.gcr.io`. A HIGH or CRITICAL finding with a fix stops the
release.

The only accepted exposures are in `platform/deployer/gcp/trivyignore.yaml`.
Each entry is a HIGH finding in a vendor binary that no vendor build fixes yet.
Each entry expires on 2026-11-30. After that date, the gate blocks each release
until a maintainer updates the tools.

`packer-gcp.yml` publishes the base guest images to the public GHCR packages
`ghcr.io/<owner>/shifter-gce-<role>`. Each release pins the newest validated
digest of each role, and the release manifest carries the authorization model
that each deploy publishes. A release stops when a base image is missing.

## Get and verify the deployer image

1. Set the release registry and the release revision:

   ```bash
   REG=ghcr.io/<owner>/<repository>
   REV=<40-hex revision>
   ```

2. Resolve the deployer image digest:

   ```bash
   DIGEST=$(oras resolve "$REG/deployer-gcp:$REV")
   ```

3. Verify the build provenance of the deployer image. This command is not yet
   confirmed against a real release; confirm it on the first release.

   ```bash
   cosign verify-attestation --type https://slsa.dev/provenance/v1 \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     --certificate-identity "https://github.com/<owner>/<repo>/.github/workflows/release-gcp.yml@<release ref>" \
     --certificate-github-workflow-sha "$REV" "$REG/deployer-gcp@$DIGEST"
   ```

4. Read the release manifest:

   ```bash
   docker run --rm --entrypoint cat "$REG/deployer-gcp@$DIGEST" /opt/bigrae/release.json
   ```

5. Run each command in this guide through the deployer image. Always use the
   digest, not the tag:

   ```bash
   docker run --rm -it --user "$(id -u):$(id -g)" -e HOME=/tmp \
     -e CLOUDSDK_CONFIG=/gcloud \
     -e GOOGLE_APPLICATION_CREDENTIALS=/gcloud/application_default_credentials.json \
     -v "$HOME/.config/gcloud:/gcloud" \
     "$REG/deployer-gcp@$DIGEST" gcp-deploy --store gs://<bucket>
   ```

The deployer image sets `BIGRAE_RELEASE_MANIFEST` and
`BIGRAE_SOURCE_REVISION`. The command uses that manifest by default. It also
checks that its own revision equals the manifest revision. A checkout must be a
clean checkout of that revision; otherwise the command stops.

`gcp-deploy` verifies the provenance of the five control-plane images before
the rollout. That check binds each image to the repository ID, the owner ID,
and the revision in `settings.release_trust` and the manifest. It then copies
each image by digest into the deployment's own Artifact Registry. The commands
send your Google credentials only to Artifact Registry, never to the release
registry.

## Deploy from a mirror

Use a mirror when the deployment cannot reach the release registry.

1. On a host that can reach both registries, copy each release image of the
   manifest to the mirror. Keep the digest and the referrers:

   ```bash
   for name in portal provisioner guacd guacamole-client openvpn; do
     SRC=$(jq -r --arg n "$name" '.images[$n]' release.json)
     oras cp --recursive "$SRC" "<mirror>/$name:release-${REV:0:12}"
   done
   ```

2. Copy the deployer image the same way to `<mirror>/deployer-gcp`.
3. Pass the mirror prefix to each command:

   ```bash
   gcp-deploy --store gs://<bucket> --release-source <mirror>
   ```

`gcp-deploy`, `gcp-destroy`, and `gcp-release-scan` accept `--release-source`.
They read each image at `<mirror>/<name>@<release digest>`. The provenance
check verifies the same digests, so a mirror cannot change the release.

## Install

1. Write the deployment input bundle in a private directory
   ([Author and publish the deployment input bundle](#author-and-publish-the-deployment-input-bundle),
   steps 1 to 4). It is the only configuration of the deployment.
2. Run the install from the deployer image of the release:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-install --inputs /path/to/bundle
   ```

3. Read the ingress address in the command output.
4. Create a DNS A record for `deployment.domain` at that address. Do not proxy
   the record.
5. Run the deploy. It waits up to 60 minutes for the managed certificate to
   become Active, then runs the smoke:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-deploy --store gs://<project>-deployment-config
   ```

`gcp-install` does these steps. Each step is idempotent, so run the same
command again after a failure:

1. It validates the bundle.
2. It applies the foundation without GitHub. It derives the inputs from the
   bundle and names your active gcloud account as the only impersonation
   principal. Pass `--operator` to name another principal.
3. It creates the initial operator password secret when it does not exist, and
   prints the command that reads it.
4. It publishes the bundle to `gs://<project>-deployment-config`.
5. It runs `gcp-deploy --first-install`. On a new deployment, that deploy:
   - imports the base range images that the release pins, as your identity;
   - generates the OpenFGA CA, server certificates, and keys into Secret
     Manager;
   - publishes the release's authorization model to OpenFGA;
   - creates the first administrator from
     `settings.operator_bootstrap.admin_email`;
   - registers the in-box catalog as that administrator.

The sections that follow describe each part, for a manual install and for
later changes.

## Apply the foundation without GitHub

1. Copy `scripts/bootstrap/gcp-foundation.github-less.example.tfvars.json` to a
   file outside the repository.
2. Set these keys:

   | Key | Value |
   | --- | --- |
   | `project_id`, `project_number` | The deployment project. The command compares the number with the project. |
   | `environment` | The deployment name. Use the same value as `deployment.name` in `bigrae.yaml`. |
   | `name_prefix` | `shifter-<environment>`. The commands derive the purpose identities from it. |
   | `impersonation_principals` | `deploy`, `release_scan`, and `destroy`, each with at least one `user:`, `group:`, or `serviceAccount:` member. |
   | `deployment_config_bucket_name` | The config bucket, for example `<project>-deployment-config`. |
   | `terraform_state_bucket_name` | `<project>-terraform-state`. The deploy command uses this name, and the foundation rejects any other name. |
   | `release_evidence_bucket_name` | `<project>-release-evidence`. The deploy command uses this name, and the foundation rejects any other name. |
   | `region` | The bucket location, for example `us-central1`. |

3. Do not set `purpose_contexts`, `github_org`, `github_repo`,
   `github_repository_id`, or `github_owner_id`. The command rejects GitHub
   inputs in this mode.
4. Preview the foundation:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-foundation --inputs /path/to/foundation.json --dry-run
   ```

5. Apply the foundation:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-foundation --inputs /path/to/foundation.json --yes
   ```

The command enables the foundation APIs and creates the private, versioned
state bucket. It then applies `platform/terraform/gcp/global/cicd-oidc` from a
saved plan. That root creates no Workload Identity pool or provider in this
mode. It creates these resources:

- the deploy, release-scan, and destroy service accounts;
- the image build service account, which this mode does not use;
- the token-creator grants for the named principals;
- the release-evidence bucket, with a locked 90-day retention policy;
- the deployment config bucket, with uniform access, public access
  prevention, and versioning;
- Cloud Audit Logs for storage reads and writes in the project. Usage-log
  delivery to the release-evidence bucket is optional
  (`enable_gcs_usage_log_delivery`), because a Domain Restricted Sharing org
  policy rejects its Google-managed writer group;
- the image-build network, only when `enable_image_build_network` is `true`.

The retention lock on the release-evidence bucket is permanent. Check the
project and the bucket name in the plan before you apply it.

The deploy and destroy identities can read the config bucket. They can change
only the `locks/` prefix. Deploy principals can write the bundle; destroy-only
principals can read it.

## Create the initial secrets

The platform Terraform creates and fills the runtime secrets: the Django key,
the database credentials, the Guacamole keys, the Redis credentials, and the
domain controller password. The deploy generates the OpenFGA credentials.
`gcp-install` creates the first operator password in
`shifter-<environment>-operator-password` when
`settings.operator_bootstrap.admin_password_secret_ref` is not set. To supply
your own password instead, create the secret first:

1. Create the secret in the deployment project:

   ```bash
   gcloud secrets create <operator-password-secret> \
     --project <project> --replication-policy=automatic
   ```

2. Add the password as a new version. Do not type it on the command line:

   ```bash
   read -rs OPERATOR_PASSWORD
   printf '%s' "$OPERATOR_PASSWORD" | gcloud secrets versions add <operator-password-secret> \
     --project <project> --data-file=-
   unset OPERATOR_PASSWORD
   ```

3. Set `settings.operator_bootstrap.admin_password_secret_ref` to the
   versionless resource `projects/<project>/secrets/<operator-password-secret>`.
   The command reads the `latest` version as the deploy identity.

The deploy identity holds the Secret Manager role in the deployment project.
Thus it can read a secret in that project. The command keeps the password in
memory only.

Other inputs that refer to secrets:

- If you set `email_backend` in the Terraform values, Terraform creates an
  empty `shifter-<environment>-email` secret. Add the provider API key to it.
- `settings.provisioner_static_secret_refs` applies only to the `gdc` range
  backend.
- `secrets.django_secret_key` in `bigrae.yaml` is a reference that the GCP
  backend schema requires.

## Author and publish the deployment input bundle

1. Create a private directory for the bundle.
2. Write `bigrae.yaml`:

   ```yaml
   version: 1
   backend: gcp
   deployment:
     name: <environment>
     domain: portal.<your-domain>
     profile: prod
   secrets:
     django_secret_key: prompt
   settings:
     project_id: <project>
     region: us-central1
     dynamic_secret_project_id: <range-secret-project>
     range_cells:
       zone: us-central1-a
     participant_openvpn:
       enabled: false
     operator_bootstrap:
       admin_email: <operator>@<your-domain>
     post_deploy_smoke:
       user_email: <smoke-account>@<your-domain>
     release_trust:
       github_repository_id: "<repository-id>"
       github_owner_id: "<owner-id>"
   ```

   | Setting | Meaning |
   | --- | --- |
   | `range_cells` | `backend` (`gce` or `gdc`), `zone`, `zones`, `project_id`, `host_identity_pool_size`, `private_google_access`, and `image_key_profiles`. A GCE range needs `zone`. |
   | `participant_openvpn.enabled` | Deploys the shared participant OpenVPN pool. The default is `false`. |
   | `ctf_content_bucket` | The private CTF content bucket, when the app secret carries CTF content references. |
   | `operator_bootstrap` | The first operator and the first administrator. `gcp-deploy` requires this block. `enabled` defaults to `true` and then requires `admin_email`. `admin_password_secret_ref` defaults to the secret that `gcp-install` creates. |
   | `openfga` | Optional. Leave it unset: the deploy generates the credentials and publishes the release model. To pin your own, set all three `*_secret_ref` values, and optionally `store_id` and `model_id`. |
   | `cloud_sql_iam_auth` | Native SQL login. It defaults to `true`, which the current release requires. |
   | `post_deploy_smoke.user_email` | An existing portal account for the smoke. An empty value skips the smoke. |
   | `release_trust` | The numeric repository ID and owner ID of the repository that publishes the release. |

   The operator email must use the domain in `identity_allowed_email_domain`.
   To get the release trust IDs, run
   `gh api repos/<owner>/<repo> --jq '.id, .owner.id'` once on any workstation.
   The deployment itself needs no GitHub access.
3. Write `terraform.tfvars.json` with the platform Terraform values that the
   command does not own:

   ```json
   {
     "gke_subnet_cidr": "10.40.0.0/20",
     "gke_pods_cidr": "10.44.0.0/16",
     "gke_services_cidr": "10.45.0.0/20",
     "gke_master_ipv4_cidr": "172.16.0.0/28",
     "gke_master_authorized_cidrs": [],
     "range_network_cidr": "10.50.0.0/16",
     "identity_allowed_email_domain": "<your-domain>"
   }
   ```

   Use `platform/terraform/gcp/environments/gcp-dev/terraform.tfvars` as the
   reference for sizing values. You can also put HCL values in
   `terraform.tfvars`. When both files exist, the JSON file loads later and its
   values win.
4. Do not set the values that the command owns. The command sets `project_id`,
   `region`, `artifact_registry_location`, `environment`, `public_hostname`,
   `enable_managed_tls`, `openvpn_pool_enabled`,
   `range_host_identity_pool_size`, `range_network_zones`, and
   `ctf_content_bucket_name` from `bigrae.yaml`. Its values win over a value in
   your files.
5. Add the optional objects only if you need them: `foundation-inputs.json`,
   `model-broker-overlay.template.json`, and `retired-cluster-artifacts.json`.
   The model broker is parked (ADR-068).
6. Publish the bundle. The command validates it first:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-config publish --store gs://<bucket> --inputs /path/to/bundle
   ```

7. Show the stored bundle. The summary has no secret values:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-config show --store gs://<bucket>
   ```

8. The deploy imports the base range images that the release pins when the
   bundle has no `range-images.json` roles. To import from another source
   instead, record the references first:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-images --project-id <project> \
     --source ghcr.io/<owner> --store gs://<bucket> --region <region>
   ```

   The command imports the `kali`, `ubuntu`, and `dc` packages as native GCE
   images and writes the `linux`, `kali`, and `dc` roles to
   `range-images.json`. Add `--dry-run` to check without a change. A GCE
   deploy requires the `linux` and `dc` roles, unless you pass
   `--allow-missing-range-images`.
9. To record one image by hand, use `set-range-image`. The roles are `linux`,
   `kali`, `windows`, and `dc`:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-config set-range-image --store gs://<bucket> \
     --role windows --image projects/<project>/global/images/<image>
   ```

10. To edit the bundle later, pull it into a private directory, edit it, and
    publish it again:

    ```bash
    ./scripts/bootstrap/deploy.py gcp-config pull --store gs://<bucket> --output /path/to/bundle
    ```

## Run the preflight

Check the tools and the bundle before you deploy. The check makes no change:

```bash
./scripts/bootstrap/deploy.py preflight --cloud gcp --env <environment> --store gs://<bucket>
```

The preflight checks the tools on `PATH`, loads the bundle, and renders the
Terraform values. It reports an insecure control-plane input, for example a
world-open authorized network. From a checkout, the tool check includes `git`.
The deployer image embeds its source revision, so the check omits `git` there.

## First install

`gcp-install` runs the first install ([Install](#install)). To run it
separately on a published bundle:

```bash
./scripts/bootstrap/deploy.py gcp-deploy --store gs://<bucket> --first-install
```

In `--first-install` mode, the command accepts a certificate that covers the
hostname but is not Active yet. It prints a DNS instruction.

On an empty database, the deploy writes the identity facts itself. The cutover
facts have no organizations, workspaces, or users to map. The bootstrap facts
name the Identity Platform account that the infrastructure stage creates for
`settings.operator_bootstrap.admin_email`. That account becomes the first
administrator, with the separate personal-credential eligibility. Other
administrators gain no implicit eligibility.

A database with users needs the reviewed legacy conversion facts. Pass them
with `--identity-cutover-facts` and `--identity-bootstrap-facts`. See
[native identity operations](../../ops/gcp-native-identity.md).

After activation, the deploy registers the release's in-box catalog as the
bootstrap administrator: it issues a credential that holds only the
installation content actions, lasts 15 minutes, and is revoked after the
registration. When the configured operator is not the bootstrap administrator,
or the registration fails, the deploy completes with a warning. Then sign in,
issue a content credential, and run the deploy again with
`--catalog-credential-file <private 0600 file>`.

## Update and roll back

1. Get and verify the deployer image of the new release
   ([Get and verify the deployer image](#get-and-verify-the-deployer-image)).
2. Change the bundle if the release needs new inputs, and publish it again.
3. Run the deploy:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-deploy --store gs://<bucket>
   ```

   The deploy publishes the release's authorization model, re-binds the
   application to it, and registers new in-box packs.

To roll back, deploy the deployer image of an earlier verified release. Do not
retag an image.

## What the deploy does

The command runs these stages in order:

| Stage | Identity | Steps |
| --- | --- | --- |
| Inputs | Your identity, then deploy | Load and validate the bundle, check the tooling revision, verify the provenance of the release images, import the release's base range images when the bundle has none, and take the lock. |
| Infrastructure | Deploy | Initialize Identity Platform, harden the state bucket, apply Terraform, verify range audit telemetry, and seed the provider account without an application grant. |
| Preconditions | Deploy | Check the GCE range inputs and images. |
| Images | Deploy | Copy each release image and its attestations into the deployment registry by digest. |
| Scan | Release scan | Scan each exact digest with Trivy for HIGH and CRITICAL findings. |
| Verify | Deploy | Verify the provenance of each image. There is no bypass. |
| Rollout | Deploy | Create the OpenFGA credentials on first use, reconcile the SQL duties, probe and drain when required, migrate storage, publish the release's authorization model, verify native activation, restore the Helm release, wait for readiness, roll out the OpenVPN pool, record the running images, and wait for the ingress and certificate. |
| Catalog | Bootstrap administrator, or a supplied native credential | Register the in-box catalog after readiness through ordinary registration, conformance and publication. A failure leaves catalog work pending with a warning; the application stays active. |
| Smoke | Deploy | Run the advisory range smoke. |

`--stage infrastructure` and `--stage rollout` run one stage. Each still
checks the tooling revision and verifies the release images before any change.
`--stage rollout` also imports and scans the release images.

Each stage sets only its purpose identity for every child process. The command
uses a private `KUBECONFIG`, so it does not change your kubeconfig contexts.
The Helm upgrade is atomic: a failed upgrade rolls back the Helm release. A
failed Terraform apply does not roll back.

## Smoke and exit status

| Exit status | Meaning |
| --- | --- |
| 0 | The deployment is current. |
| 1 | A stage failed. The command printed the reason. |
| 2 | The command arguments are not valid. |
| 3 | The deployment is current, but the advisory smoke failed. |

Add `--no-smoke` to skip the smoke. Run the smoke alone with:

```bash
./scripts/bootstrap/deploy.py gcp-smoke --store gs://<bucket>
```

## Evidence

The commands write evidence to `<project>-release-evidence`. Use
`--evidence-bucket` for a different bucket.

| Prefix | Writer | Objects |
| --- | --- | --- |
| `release-scans/<revision>/<run id>/<attempt>/` | Release-scan identity | `release-manifest.json`, one `<image>-trivy.json` for each image, and `verdict.json` |
| `deployments/<revision>/<run id>/<attempt>/` | Deploy identity | `running-image-ids.json` and `verdict.json` |

`verdict.json` holds the result, an opaque locator, and the SHA-256 of the raw
evidence files in its prefix. The digest does not include `verdict.json`. The
command makes a run id from the time and a random suffix. Set `--run-id` and
`--attempt` to use your own values.

## Deployment lock

The command creates `locks/deploy.lock` in the config bucket before it changes
anything. It removes the lock at the end, also when a stage fails. A second
command fails while the lock exists and names the holder.

1. Make sure that no other deploy or destroy runs for the deployment.
2. Remove a stale lock with `--break-lock` on the next command. The command
   removes the lock with no prompt:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-deploy --store gs://<bucket> --break-lock
   ```

## Tear down

1. Get the deployer image of the release that the deployment runs.
2. Run the destroy. `--confirm` must equal the deployment name:

   ```bash
   ./scripts/bootstrap/deploy.py gcp-destroy --store gs://<bucket> --confirm <environment>
   ```

The destroy runs as the destroy identity. It first verifies that the tooling
revision equals the release revision and that the release images verify. It
then takes the lock, lifts the Cloud SQL deletion protection, removes the
permanent KMS key ring and key from the state, and runs `terraform destroy`
with bounded retries.

These items stay after a teardown:

- the foundation: the purpose identities, the state bucket, the
  release-evidence bucket, and the config bucket;
- the Artifact Registry KMS key ring and key;
- the resources that Terraform does not own, for example the operator password
  secret and the imported range guest images.

## Identity options

| Option | Use |
| --- | --- |
| `--name-prefix` | The purpose identity prefix when it is not `shifter-<environment>`. |
| `--deploy-service-account`, `--release-scan-service-account`, `--destroy-service-account` | An explicit purpose identity. With an explicit deploy identity, the command also reads the bundle as that identity. |
| `--environment` | The deployment name when it is not `deployment.name`. |

A derived identity is `<name prefix without hyphens>-deploy`, `-scan`, or
`-destroy` at `<project>.iam.gserviceaccount.com`. A service account ID has 30
characters or fewer, so a derived name can be too long. The command then stops
and tells you to pass the identity explicitly.
