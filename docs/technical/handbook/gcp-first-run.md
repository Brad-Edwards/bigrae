# GCP installation and first exercise

Use this route from an empty deployment to a small verified range. Commands run
from the repository root. The
[GitHub-less GCP deployment guide](gcp-github-less-deployment.md) owns the exact
commands; the [full setup](../dev/setup.md#gcp-deployment) owns prerequisite
details; the [handbook](../architecture.md) explains the boundaries.

## Prepare the deployment

Have Python 3.12+, uv, gcloud, and Docker available. The deploy runs from the
deployer image of a published release. A deploy from a checkout instead needs
Terraform, Helm, kubectl, gke-gcloud-auth-plugin, ORAS, Trivy, and git on
`PATH`; `deploy.py preflight --cloud gcp` checks them. A deployment without GitHub needs
no GitHub CLI. Use a billing-enabled project and domain under your control.
An authorized operator must do these steps, as the guide and
[deployment secrets](../../dev/deploy-secrets.md) describe:

- create the foundation: the purpose identities, the impersonation grants, and
  the deployment config bucket;
- grant read access to the release registry;
- store the first operator password in Secret Manager;
- own the DNS of the public hostname.

Do not put credentials in commands, source control or examples. Configuration
stores secret references.

```bash
uv run --project shifter/installation bigrae-config init --backend gcp
# Edit the generated bigrae.yaml for the selected deployment.
uv run --project shifter/installation bigrae-config validate bigrae.yaml
uv run --project shifter/installation bigrae-config doctor bigrae.yaml
```

`init` scaffolds local configuration; it does not create a cloud account,
identity, cluster or secret. For a new foundation, run `gcp-foundation` in its
GitHub-less mode
([Apply the foundation without GitHub](gcp-github-less-deployment.md#apply-the-foundation-without-github)).
The [bootstrap guide](https://github.com/Brad-Edwards/bigrae/blob/dev/scripts/bootstrap/README.md)
lists the checked inputs and the apply order.
Put `bigrae.yaml` and the Terraform values of the deployment in one directory;
stale tenant values from another deployment must not be reused.

Once these prerequisites and the range-plane settings are configured, publish
the deployment inputs and import the range images:

```bash
./scripts/bootstrap/deploy.py gcp-config publish \
  --store gs://YOUR_CONFIG_BUCKET --inputs ./YOUR_INPUTS_DIRECTORY
./scripts/bootstrap/deploy.py gcp-images --project-id YOUR_PROJECT \
  --source YOUR_RELEASE_REGISTRY --region YOUR_REGION \
  --store gs://YOUR_CONFIG_BUCKET
```

Then verify the deployer image of the release and run
`gcp-deploy --store gs://YOUR_CONFIG_BUCKET --first-install` from it, as the
guide describes
([Get and verify the deployer image](gcp-github-less-deployment.md#get-and-verify-the-deployer-image),
[First install](gcp-github-less-deployment.md#first-install)). Replace the
uppercase placeholders.
`settings.range_cells.backend` selects the range backend. The default is `gce`:
GKE hosts the control plane and GCE hosts guests. `gdc` is an
operator-validation substrate, not a participant deployment shortcut. The
deploy creates billable resources; run it only in your authorized environment.
The live test of this path is issue #2572. Updates run `gcp-deploy` again from
the deployer image of a newer release. A CI-deployed environment runs the same
command through the deploy workflow.

After rollout and DNS/TLS convergence, run:

```bash
uv run --project shifter/installation bigrae-config doctor bigrae.yaml --checks cloud
```

This checks configured health probes, not scenario acceptance. For a failed
deploy or update, use [RB023](../../runbooks/rb023-gcp-deployment.md): inspect
bounded diagnostics, and confirm the selected tenant and configuration. Do not
conceal an unknown cleanup state by deleting application records.

## Run a useful scenario

The shipped synthetic
[smoke-linux pack](https://github.com/Brad-Edwards/bigrae/tree/dev/shifter/environment-packs/smoke-linux)
is the initial GCP RAES path. The
[in-box manifest](https://github.com/Brad-Edwards/bigrae/blob/dev/shifter/shifter_platform/cms/scenarios/inbox_packs/manifest.yaml)
registers it through the normal CMS validator during catalog bootstrap. It is a
bootstrap seed, not a promise of a full production scenario catalog.

1. Sign in as the initialized operator and confirm that the pack is registered
   and realizable for the selected backend and exact image inventory.
2. Select the registered scenario through the range UI. Launch with an authorized
   principal in the actual resource scope; wait for Engine's READY projection.
3. Open the authorized terminal/access surface and exercise the expected guest
   behavior. A successful Job alone is not guest-readiness evidence.
4. Destroy through the same owning lifecycle API/UI. Confirm verified absence
   and resolve any residual cleanup before releasing event capacity.

For your own current RAES pack, use the
[registration/preparation guide](../../ops/artifact-preparation.md), public
adapter SDK and administrator-controlled installation. Never copy private
content or answers into core. For native CTF events, use the
[organizer guide](../../features/ctf-organizer-guide.md) and
[digest-bound content guide](../../dev/ctf-scenario-content.md).

The [GCE deployment runbook](../../dev/gcp-range-cell-deploy.md) lists image,
network and access prerequisites. Domain scenarios require compatible prepared
images and actual guest verification. GCE does not supply NGFW attachment;
GDC or Pods are not an automatic fallback. Contract tests, a deploy health check
and one smoke range do not qualify every scenario, release or event capacity.
