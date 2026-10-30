# AWS native identity operations

This guide covers the AWS bindings of the BigRAE identity model (#2313). The
model, permissions and credential rules are the same as on GCP. Only the
provider bindings change. Source tests do not prove live IAM, renewal or
rotation. Qualify each binding on the installation before you rely on it.

## Enable the account issuer

Service principals prove their identity with an AWS STS web identity token.
Apply `platform/terraform/global/iam`. It enables IAM outbound web identity
federation for the account. You cannot use service principals before this step.
The EKS root reads the issuer and gives it to the portal.

## Configure the policy service

Set `settings.openfga` in the AWS installation config:

| Field | Value |
| --- | --- |
| `enabled` | `true` |
| `store_id`, `model_id` | Both blank for the first publication, or both published ULIDs. |
| `runtime_secret_ref`, `admin_secret_ref`, `ca_secret_ref` | Three distinct Secrets Manager secret ARNs. |

The runtime and admin secrets each hold only `server-preshared-keys`,
`api-token`, `tls.crt` and `tls.key`. Use separate key sets and TLS keys. The
CA secret holds only `ca.crt`. Create these secrets with your operator secret
workflow. Never put their values in installation settings, Helm values,
Terraform outputs or command arguments.

Terraform creates the datastore logins and their secrets. Do not create them
yourself.

## Deploy

Run `deploy.py eks-deploy` with the usual config, images, backend and Terraform
inputs. The command does these steps in this order:

1. It downloads the region's public RDS CA bundle from the AWS trust store.
2. It builds the datastore rotation function from its hash-pinned lock.
3. It applies the EKS Terraform root. Terraform uploads the package, AWS Signer
   signs it, and Lambda accepts only the signed package.
4. It publishes the policy bundles and the datastore logins as named
   Kubernetes Secrets, through standard input only.
5. It creates the Guacamole and OpenFGA databases and the runtime SQL logins.
   On the first deploy, it then applies Terraform again to create the rotation
   schedules, because Secrets Manager tests each new schedule at once.
6. It runs the identity cutover ceremony, migrations and the release. The
   policy servers restart in each deploy, so republished credentials take effect.
7. It runs catalog setup.

For a first installation or a legacy conversion, supply
`--identity-cutover-facts` and `--identity-bootstrap-facts` as private 0600
files. The [GCP guide](gcp-native-identity.md#live-acceptance-evidence) describes
their content. It applies to AWS without change.

Catalog setup needs `--catalog-credential-file`, a private 0600 file with a
current native bearer proof. Without it, the command activates the application,
then reports catalog setup as incomplete. Sign in as the administrator, issue a
credential, then run the command again with the file.

## Publish the policy store and model

With blank store and model IDs, the policy servers start and the portal client
stays unbound. Open the operator tunnel (below). Then port-forward
`service/openfga-admin` and use the released OpenFGA CLI with the admin
credential and the CA. Publish the model from `manage.py render_openfga_model`.
Record the IDs in `settings.openfga` and deploy again.

## Admit a service principal

1. Create or choose an IAM role for the service. Do not use an IAM user.
2. Give the role `sts:GetWebIdentityToken` with conditions on
   `sts:IdentityTokenAudience` (the portal origin) and
   `sts:SigningAlgorithm` (`ES384`).
3. As an administrator, register the admission with the role ARN as the
   subject and the needed scopes.
4. Grant application policy to the service principal. Admission alone grants
   nothing.
5. In the service, use `ServiceClient.from_aws(audience)` from the
   `bigrae-client[aws]` package. It renews the token before expiry.

Anyone who can assume the role is that principal. Keep the role trust policy
exact. A role that is deleted and created again with the same name keeps the
same principal. Remove the admission when you retire a role.

## Operator access to the cluster

The EKS API endpoint is private. Set `operator_principal_arns` in
`platform/terraform/global/iam` to the exact IAM user or role ARNs that may
assume the deploy role:

- An IAM user must sign in with MFA and set its user name as the source
  identity.
- An IAM role, for example a federated workforce role, needs only its exact ARN.

The tunnel and `kubectl` must both use the deploy role's credentials. Get them
into your shell first.

An IAM role operator adds a role profile and selects it:

```ini
[profile shifter-deploy]
role_arn = <deploy-role-arn>
source_profile = <your-profile>
```

```console
export AWS_PROFILE=shifter-deploy
```

An IAM user operator must set a source identity. AWS CLI role profiles have no
source identity setting, so export the assumed-role credentials instead:

```console
unset AWS_PROFILE
eval "$(aws sts assume-role --role-arn <deploy-role-arn> --role-session-name <your-user-name> \
  --source-identity <your-user-name> --serial-number <mfa-device-arn> --token-code <code> \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text \
  | awk '{print "export AWS_ACCESS_KEY_ID="$1" AWS_SECRET_ACCESS_KEY="$2" AWS_SESSION_TOKEN="$3}')"
```

Confirm that `aws sts get-caller-identity` shows the deploy role. Then, in the
same shell, open a tunnel through the operator access node:

```console
aws ssm start-session --target <operator_access_instance_id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters host=<private-api-host>,portNumber=443,localPortNumber=8443
```

In a second shell with the same credentials, write a kubeconfig and use it
through the tunnel:

```console
aws eks update-kubeconfig --name <cluster-name> --region <region> --kubeconfig <file>
kubectl --kubeconfig <file> --server https://127.0.0.1:8443 \
  --tls-server-name <private-api-host> get nodes
```

The kubeconfig runs `aws eks get-token`, which uses the deploy role's
credentials from the environment. The EKS root outputs the node ID. Session
Manager does not log the content of port-forward sessions. CloudTrail records
each session start.

## Retire the legacy administrator users

Releases before #2313 included `platform/terraform/global/se-admins`. That root
created IAM users with console passwords and `AdministratorAccess`. Removing
the root from the source does not delete users that an account applied. In
each account that applied it, destroy those users once:

1. Check out a revision that still contains the root, for example the `dev`
   commit before this change, in a separate worktree.
2. In `platform/terraform/global/se-admins`, run `terraform init` with that
   account's backend configuration. The state key is
   `global/se-admins/terraform.tfstate`.
3. Run `terraform destroy` with the same `admins` variables that were applied.
4. Confirm that `terraform state list` prints nothing.
5. Confirm that `aws iam get-user --user-name <name>` returns `NoSuchEntity`
   for each former administrator.

## Rotation

The datastore passwords rotate every 30 days. The runtime and admin logins
alternate between two users, so the previous password stays valid until the
next rotation. The `openfga-credential-refresh` CronJob copies the current
passwords into Kubernetes each week and restarts the policy servers. The
migration login rotates its own password. Each deploy republishes it before
migrations run.

After a failed rotation, read the rotation function logs under
`/aws/eks/<cluster>/openfga-rotation-*`. Fix the cause, then retry the rotation
from Secrets Manager.

## Known limits

- A global sign-out done only in Cognito ends a portal session at the absolute
  session limit. Disabling or deleting the user ends it at the next recheck.
- The rotation function trusts the RDS CA bundle in its package. Qualify one
  rotation after each change to the bundle.
- Account scope is an application boundary. It does not prove separate
  database, network or key boundaries.
