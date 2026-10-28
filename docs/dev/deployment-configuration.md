# Deployment configuration and recovery

## GCP source and state

The platform has one Terraform execution root: `platform/terraform/gcp/environments/platform`.
The `gcp-deploy` command runs it for the CI path and for the path without GitHub (ADR-070).
It writes these var files into a private directory and passes them in this order:

1. `terraform.tfvars`, then `terraform.tfvars.json`, from the deployment input bundle;
2. `settings.auto.tfvars`, rendered from the `bigrae.yaml` settings;
3. `zz-deployment.auto.tfvars.json`, with the values that `bigrae.yaml` owns.

A later file wins, so the values that `bigrae.yaml` owns win over a conflicting operator value.
The command uses a private `TF_DATA_DIR` for each run.

A deployment without GitHub keeps its bundle in its deployment config bucket, not in this repository.
A CI deployment keeps its values in `environments/<environment>/terraform.tfvars`.
The CI adapter copies that file into the bundle and adds the GitHub secret values in `terraform.tfvars.json`.

The state location remains the existing bucket and `shifter/<environment>/platform-core` prefix.
Module names, resource addresses, provider locks and outputs are unchanged.
Changing the source directory requires no resource import or `terraform state mv`.
Foundation identity and runner state remain separate.

For manual operations on a CI deployment, select values and metadata before initialization:

```bash
environment=gcp-dev
values_dir="$PWD/platform/terraform/gcp/environments/$environment"
export TF_DATA_DIR="$values_dir/.terraform"
root=platform/terraform/gcp/environments/platform

terraform -chdir="$root" init -reconfigure \
  -backend-config="bucket=<existing-state-bucket>" \
  -backend-config="prefix=shifter/$environment/platform-core"

terraform -chdir="$root" plan \
  -var-file="$values_dir/terraform.tfvars" \
  -var-file="$values_dir/local.auto.tfvars"
```

Supply every operator override that the environment previously used.
Keep generated range settings last when they are part of the deployment.
Pass the same files to apply, import and destroy.
Never share `TF_DATA_DIR` between environments.
Do not put environment values or credentials in the shared root.

## Verification before reuse

The operator confirmed no deployed BigRAE instances during this cleanup.
The retired AWS environment and standalone workshop resources were confirmed drained.
No cloud apply, destroy or state mutation is part of the source cleanup.
Backendless validation and identical rendered manifests check configuration compatibility.
They do not prove a live no-change migration.

Before reusing retained infrastructure state, run a locked plan with its original inputs.
The plan must report no changes before a source-only cutover.
Investigate drift, missing inputs or replacement proposals before proceeding.
An empty-state plan normally proposes resource creation.
That result is not a failed migration or a no-change result.
Keep full plans and operational evidence private.

## Retained AWS surfaces

The `platform/terraform/environments/prod` roots have owner @Brad-Edwards.
Their callers are `scripts/bootstrap/deploy.py terraform|full --env prod --profile <profile>`
and the EKS deployment lifecycle.
AWS production is not a workflow deployment choice.
Use the [AWS EKS bundle](../technical/platform_infrastructure/aws-eks-bundle.md) for application deployment.

The optional portal CTFd module has the same owner.
The portal root calls it through `module.ctfd` when enabled.
Complete setup through upstream CTFd administration.
The removed standalone workshop is separate from this retained module.

## Earlier cleanup and recovery

The last source revision before this cleanup is `b174cdea8371c8ce4624c73ce5c5a04d446a8c41`.
It retains the retired build and infrastructure definitions.
Use that immutable revision when investigating resources created by the earlier release.
Source deletion never deletes cloud resources.

An operator can export that revision into a private recovery directory:

```bash
revision=b174cdea8371c8ce4624c73ce5c5a04d446a8c41
recovery_dir=<private-recovery-directory>
mkdir -p "$recovery_dir"
chmod 700 "$recovery_dir"
git archive "$revision" | tar -x -C "$recovery_dir"
```

Review the earlier root and provider lock before using it.
Select the original backend and original private inputs.
Back up state privately before any destructive action.
Review a destroy plan before authorizing cleanup.
Retain shared foundation, identity, runner and state resources until their dependents are removed.
Do not copy state, credentials or private recovery evidence into this repository.

## Repository identity after a rename

Use the canonical repository in bootstrap, workflow trust inputs and artifact verification.
Use attestations bound to that identity.
Rebuild images if their earlier attestations name the former identity.
Keep exact repository and immutable workflow checks in place.
Retain the immutable owner and repository IDs in trust inputs and scanner admission.
A redirect is not an additional trusted signing identity.
