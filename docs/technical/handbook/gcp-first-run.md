# GCP installation and first exercise

Use this route from an empty deployment to a small verified range. Commands run
from the repository root. The [full setup](../dev/setup.md#gcp-deployment) owns
prerequisite details; the [handbook](../architecture.md) explains the boundaries.

## Prepare the deployment

Have Python 3.12+, uv, Terraform 1.7+, Docker, gcloud, kubectl, Helm and GitHub
CLI available. Use a billing-enabled project and domain under your control.
An authorized operator must configure APIs, purpose-specific Workload Identity
Federation, protected GitHub Environments, image access, DNS and the first
operator credential as described in [setup](../dev/setup.md#gcp-deployment) and
[deployment secrets](../../dev/deploy-secrets.md). Do not put credentials in
commands, source control or examples. Configuration stores secret references.

```bash
uv run --project shifter/installation shifter-config init --backend gcp
# Edit the generated shifter.yaml for the selected deployment.
uv run --project shifter/installation shifter-config validate shifter.yaml
uv run --project shifter/installation shifter-config doctor shifter.yaml
```

`init` scaffolds local configuration; it does not create a cloud account,
identity, cluster or secret. For a new foundation, follow the checked inputs and
apply order in the
[bootstrap guide](https://github.com/Brad-Edwards/shifter/blob/dev/scripts/bootstrap/README.md).
The full setup also explains the backend Terraform overrides; stale tenant
values in an existing override must not be reused for a new deployment.

Once these prerequisites and range-plane settings are configured, the existing
operator bootstrap command is:

```bash
./scripts/bootstrap/deploy.py gdc-bootstrap \
  --project-id YOUR_PROJECT --environment YOUR_ENVIRONMENT \
  --region YOUR_REGION --zone YOUR_ZONE \
  --shifter-config ./shifter.yaml --import-public-base-images --yes
```

Replace the uppercase placeholders. Despite its name, the default range backend
is `gce`: GKE hosts the control plane and GCE hosts guests. `--range-backend gdc`
is an operator-validation substrate, not a participant deployment shortcut.
Bootstrap creates billable resources and publishes configured images to the
selected deployment; run it only in your authorized environment. This handbook
change has not executed it. Regular updates use the existing deploy workflow,
not an independent hosting path.

After rollout and DNS/TLS convergence, run:

```bash
uv run --project shifter/installation shifter-config doctor shifter.yaml --checks cloud
```

This checks configured health probes, not scenario acceptance. For failed
bootstrap or upgrades, inspect bounded diagnostics, confirm the selected tenant
and configuration, and follow the full setup/recovery procedure. Do not conceal
an unknown cleanup state by deleting application records.

## Run a useful scenario

The shipped synthetic
[smoke-linux pack](https://github.com/Brad-Edwards/shifter/tree/dev/shifter/environment-packs/smoke-linux)
is the initial GCP RAES path. The
[in-box manifest](https://github.com/Brad-Edwards/shifter/blob/dev/shifter/shifter_platform/cms/scenarios/inbox_packs/manifest.yaml)
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
