# GCP tenant standup gotchas

Part of the BigRAE deploy and operations docs; start at the [documentation home](../index.md).

A running log of operator gotchas hit while standing up or re-pointing a GCP
BigRAE tenant that are **not** fixed in code, only in operator procedure. Fixes
that belong in the scripts, workflows, or Terraform go into the code, not here.
Use this alongside the authoritative runbooks:
[`deploy-secrets.md`](deploy-secrets.md),
[`gcp-range-cell-deploy.md`](gcp-range-cell-deploy.md),
[`setup.md`](../technical/dev/setup.md),
[`gcp-guest-images.md`](../architecture/gcp-guest-images.md),
the [GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md),
[RB023](../runbooks/rb023-gcp-deployment.md), and
[`scripts/bootstrap/README.md`](https://github.com/Brad-Edwards/bigrae/blob/dev/scripts/bootstrap/README.md).

A fresh tenant without GitHub follows the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md):
`gcp-foundation` in GitHub-less mode, then `gcp-deploy` from the release's
deployer image. The deploy command runs Terraform as the deploy identity through
impersonation. The ADC quota-project note in `scripts/bootstrap/README.md`
applies only to a manual Terraform run under your own Application Default
Credentials.

## Release from dev or a deploy-test branch

`release-gcp.yml` publishes from `refs/heads/main`, `refs/heads/dev`, and
`refs/heads/deploy-test`. A Release Please release on `main` starts it
automatically. GitHub dispatches a workflow only after the file is on the
default branch. After `release-gcp.yml` is on `main` once, dispatch it on
another release ref:

```bash
gh workflow run release-gcp.yml --ref deploy-test
```

The run uses the workflow file of that ref. Each release ref needs a deployment
branch policy in the `gcp-release` Environment. Protect `deploy-test` against
force pushes and deletion. The release goes to the public registry
`ghcr.io/<owner>/<repository>`, so every deployment can use it.

`gcp-deploy` requires that its own revision equals the release revision. A deploy
fix therefore needs a new release. Put the fixes on `deploy-test` and release
from it, so that a test cycle does not wait for a merge to `dev`.

## Hand-patch a checkout deploy during a test cycle

`gcp-deploy` reads `BIGRAE_SOURCE_REVISION` before it reads `git`. The deployer
image sets it. To test a fix before the next release, cherry-pick the fix into
a worktree of the release revision and set the variable to that revision:

```bash
BIGRAE_SOURCE_REVISION=<release revision> \
  uv run --project scripts/bootstrap scripts/bootstrap/deploy.py gcp-deploy \
  --store gs://<bucket> --release-manifest release.json
```

The release images and their provenance stay those of the release. Use this
only for a fix that is already in a pull request, and release again after it
merges to `deploy-test`.

## Guest images must exist in the release registry

`release-gcp.yml` takes the newest guest image of each role from
`ghcr.io/<owner>/<repository>/gce-<role>`. The release stops when a role is
missing or its provenance does not verify against this repository. After a
repository transfer, publish the guests again with `packer-gcp.yml` from the
new repository, and make the packages public.

## Make new release packages public

GHCR makes a new package private. After the first release from a repository,
make the `portal`, `provisioner`, `guacd`, `guacamole-client`, `openvpn`, and
`deployer-gcp` packages public in the GitHub package settings. Until then, a
deploy cannot pull the release images.

## The foundation needs project IAM administration

`roles/editor` is not enough for `gcp-install` or `gcp-foundation`. Editor creates the service
accounts and buckets, but it cannot set IAM policy, create custom roles, or
change the audit configuration. The apply then stops partway with `403`
errors. Get `roles/owner`, or `roles/resourcemanager.projectIamAdmin`,
`roles/iam.roleAdmin`, and `roles/iam.serviceAccountAdmin`, and run the same
command again. It continues from the resources that it created.

## A foundation with release purpose contexts

Releases publish to the public registry with the workflow token, so the
`cicd-oidc` root no longer accepts a `release` purpose context. A foundation
applied before that change must remove its `release` contexts from
`purpose_contexts` before its next `gcp-foundation` run.

## Identity facts are only for an existing database

On an empty database, the deploy writes the identity facts itself from
`settings.operator_bootstrap.admin_email` (#2679). Pass
`--identity-cutover-facts` and `--identity-bootstrap-facts` only to convert a
database that already has users. A failed first install that stopped inside the
cutover resumes on the next run.

## Manual Terraform runs and a stale ADC quota project

`gcp-foundation` sets the quota project of its child processes. A manual
Terraform run uses the quota project in your ADC file. If that project is deleted, each call fails with the error `The requested
project was not found`. Set the quota project and a private data directory for the run:

```bash
export GOOGLE_CLOUD_QUOTA_PROJECT=<project> TF_DATA_DIR="$(mktemp -d)"
```

## Read the release manifest without Docker

A checkout deploy needs `--release-manifest`. The deployer image holds the
manifest at `/opt/bigrae/release.json`, and the release run summary prints it.
Without Docker, extract it from the image layer with ORAS:

```bash
REF=<release registry>/deployer-gcp@<digest>
IMAGE=$(oras manifest fetch "$REF" | jq -r '.manifests[0].digest')
for layer in $(oras manifest fetch "${REF%@*}@$IMAGE" | jq -r '.layers[] | select(.size < 200000) | .digest'); do
  oras blob fetch "${REF%@*}@$layer" --output - | tar -xzO opt/bigrae/release.json 2>/dev/null > release.json && break
done
```

The release registry is public, so ORAS needs no credentials for it. For an
Artifact Registry mirror, ORAS uses the Docker credential helper, so
`docker-credential-gcloud` must be on `PATH`.

## Range images that are already in the project

The deploy imports the base range images that the release pins when the bundle
has no range image roles. If other base guest images already exist in the
project, record them with `gcp-config set-range-image` for the `linux`,
`kali`, and `dc` roles first; the deploy then imports nothing.

## Use the deployer image for the tools

The deployer image holds every deploy tool at the release version. Without it,
`gcp-deploy` requires `gcloud`, `gke-gcloud-auth-plugin`, `terraform`, `kubectl`,
`helm`, `oras`, and `trivy` on `PATH`. It stops before any change when one is
missing. Use Helm 3: the deployer image and the CI deploy job use `v3.22.1`, and
Helm 4 is a separate major release that the chart is not exercised against.
`kubectl`, `helm`, and `oras` install as static binaries into a `PATH` directory
without root.

## Installing `gke-gcloud-auth-plugin` without root

The deploy command requires `gke-gcloud-auth-plugin` on `PATH`; the deployer
image already holds it. On a Debian/apt `gcloud`, the component manager is
disabled, so `gcloud components install` fails. Install the plugin without root
by extracting the apt package:

```bash
cd "$(mktemp -d)"
apt-get download google-cloud-cli-gke-gcloud-auth-plugin
dpkg-deb -x google-cloud-cli-gke-gcloud-auth-plugin_*.deb ./x
cp ./x/usr/lib/google-cloud-sdk/bin/gke-gcloud-auth-plugin ~/.local/bin/
```

## Range inputs before the first deploy

The GCE range precondition gate stops the deploy when a required range input is
missing. It requires `settings.range_cells.zone`, the `linux` and `dc` roles in
`range-images.json`, and the Terraform output `range_host_service_account_email`.
Each image in `range-images.json` must exist in the range project. A release
that pins base images supplies them; for an older release, import the images
with `gcp-images --store` before the first deploy. For a deliberate
platform-first bring-up, pass `--allow-missing-range-images`. Do not set the
range host identity; the command reads it from the Terraform output.

## GCE stockout on the provisioner node pool

The regional cluster places one provisioner node per zone. A zone can return
`GCE_STOCKOUT` for the configured `provisioner_machine_type` and leave the node
pool in `RUNNING_WITH_ERROR`, failing the apply after the roughly 35-minute
node-pool timeout, while every other pool comes up. The stockout is
machine-type and zone specific. Confirm an alternative type has capacity in all
of the cluster's zones (the `access` pool already proves `e2-standard-8`
availability), then set `provisioner_machine_type` to that type of the same
size class (`e2-standard-8` is the same 8 vCPU / 32 GB shape as
`n2-standard-8`). The pool runs only the provisioner launcher and the
Terraform provisioner Jobs, so a smaller E2 type, for example `e2-standard-4`,
also works. After a stockout, the pool stays `RUNNING_WITH_ERROR` and the apply
fails at the node-pool timeout. Publish the new type in the bundle and run the
deploy again; Terraform replaces the pool. Set it in the bundle's Terraform values (`terraform.tfvars`
or `terraform.tfvars.json`). A CI deployment sets it in its committed
`environments/<environment>/terraform.tfvars`. Do not try to pin the GKE node
zones: there is no tfvar for it, so that would be a module change, not an
operator override.

## Use Connect Gateway credentials for your own cluster checks

The deploy command uses its own private `KUBECONFIG`, so your kubeconfig
contexts do not change its context. `gcloud container clusters get-credentials`
writes the private control-plane endpoint, which the workstation cannot reach
when `gke_master_authorized_cidrs = []`. For your own checks, use a separate
`KUBECONFIG` and Connect Gateway credentials:

```bash
KUBECONFIG=/tmp/tenant-check gcloud container fleet memberships get-credentials \
  <cluster> --project <project>
KUBECONFIG=/tmp/tenant-check kubectl -n shifter-platform get pods
```

## DNS record and the Google-managed certificate

The public hostname needs a DNS-only (unproxied) A record at the ingress IP
before the Google-managed certificate leaves `Provisioning`. The ingress IP is
the `shifter-<environment>-platform-ip` global address (the deploy command also
prints it as the ingress address). On a Cloudflare-managed zone, watch for a stale
record left by a prior tenant: a hostname reused from a torn-down environment can
still hold a CNAME to a dead load balancer, which blocks creating the A record
until you convert it. Keep the record unproxied so the load balancer can serve
the certificate challenge. Run the first deploy with `--first-install`; it
finishes when the certificate covers the hostname, before the certificate is
`ACTIVE`. First issuance can take longer than one deploy run. After the
certificate is `ACTIVE`, run `gcp-deploy` again without `--first-install`; a
normal deploy waits up to 60 minutes for an `ACTIVE` certificate. The domain
status can show `FailedNotVisible` for a while after the A record resolves,
because Google checks the record again on its own schedule. In one standup it
changed to `ACTIVE` about 45 minutes after the record resolved. No action is
needed while the hostname resolves to the ingress address and the load balancer
answers on port 80.

## A destroy releases the ingress address

The `shifter-<environment>-platform-ip` global address is part of the platform
Terraform, so `gcp-destroy` releases it. A fresh install after a destroy can get
a different address; one reinstall got the same address again. Update the DNS A record to the address that the `--first-install`
run prints before you run the deploy again.

## Pin the gcloud account for a long run

The commands use the active gcloud account to impersonate the purpose
identities. If another shell changes the active account during a run, the next
gcloud call in the run fails, for example the lock release at the end of a
destroy. Pin the account for the run instead of relying on the global setting:

```bash
CLOUDSDK_CORE_ACCOUNT=<operator> uv run --project scripts/bootstrap scripts/bootstrap/deploy.py gcp-deploy --store gs://<bucket>
```

## Credentials expire during a long run

A test cycle can run for several hours. The gcloud user credentials and the
Application Default Credentials (ADC) can expire during that time. Then the
next gcloud call fails with `Reauthentication failed. cannot prompt during
non-interactive execution`. Before each deploy or destroy, check both
credentials:

```bash
CLOUDSDK_CORE_ACCOUNT=<operator> gcloud auth print-access-token > /dev/null
gcloud auth application-default print-access-token > /dev/null
```

If a check fails, run `gcloud auth login` or `gcloud auth application-default
login` again. Then run the stopped command again. Use `--break-lock` only if
the stopped run held the deployment lock.

## Deploy from a checkout without Docker

Without Docker, run the commands from a worktree of the release revision.
`gcp-deploy` stops when `HEAD` is not the release revision or when tracked
files have changes. Use a separate worktree for each release, so that other
work does not change it:

```bash
git worktree add --detach <path> <release revision>
cd <path>
uv run --project scripts/bootstrap scripts/bootstrap/deploy.py gcp-deploy \
  --store gs://<bucket> --release-manifest release.json
```

Install the tool versions that `platform/deployer/gcp/Dockerfile` pins.

## Never commit a deployment identity

Do not put a real project ID or service-account email into the chart values
(`platform/charts/shifter/values*.yaml`) or into any other committed file. The
real deploy identity is reconnaissance-sensitive and tenant-specific (ADR-004-R14,
ADR-011). The deploy command renders the image repositories and workload
identities from the Terraform outputs into private generated values at deploy
time. The `no-live-gcp-deploy-identity` adr-guard check scans the chart values.

## Shared Terraform source

The execution root is `platform/terraform/gcp/environments/platform`.
`gcp-deploy` passes the bundle's values and its generated var files, and it
isolates `TF_DATA_DIR` automatically.
Manual commands must select the values and `TF_DATA_DIR` explicitly.
Follow [deployment configuration](deployment-configuration.md) before reusing retained state.
