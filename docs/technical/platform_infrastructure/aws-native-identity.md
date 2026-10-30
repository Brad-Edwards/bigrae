# AWS native identity implementation

This contract records how the AWS backend binds the shared identity model
(#2313). The [design](../../design/aws-native-identity-integration-2313.md)
records the owner decisions and the threat model.
[ADR-066](../../adr/066-account-principal-resource-scope.md) is the binding model.

## Service principal proof

`config.service_identity` selects the verifier from `CLOUD_PROVIDER`. On AWS it
calls `shared.aws_web_identity.verify_aws_web_identity_token`. The verifier
fetches signing keys from the configured issuer's `/.well-known/jwks.json` and
requires:

- an ES384 or RS256 signature;
- `iss` equal to `AWS_SERVICE_TOKEN_ISSUER`;
- `aud` equal to `AWS_SERVICE_TOKEN_AUDIENCE`, as one value;
- `sub` that is an IAM role ARN in `AWS_ACCOUNT_ID`;
- the namespaced `aws_account` claim equal to `AWS_ACCOUNT_ID`;
- `exp`, `iat` and `jti`, with a lifetime of 3600 seconds or less.

Any failure is one bounded denial. The composition root binds a
`shared.service_proof` profile, so `management.service_credentials` validates
admission subjects without provider literals. The binding is `(issuer, role ARN)`.
`ProviderBinding.subject` holds up to 2048 characters, the IAM ARN maximum.

`shifter_client.ServiceClient.from_aws` obtains tokens with STS
`GetWebIdentityToken` (ES384, 900 seconds) and renews them 60 seconds before
expiry. The botocore credential chain renews the source role credentials.

## Human sessions

`ShifterOIDCBackend` requires `auth_time`. A Cognito login stores issuer, subject
and `auth_time` in the session through the login signal. `session_credentials`
applies the idle and absolute limits to Cognito sessions. At the recheck
interval, `config.cognito_directory` calls `AdminGetUser` on the pool that the
issuer names. A disabled, deleted, unconfirmed or unverified user, or a subject
mismatch, ends the session. A provider failure ends it too.

## Datastores

| Process | Login | Authentication |
| --- | --- | --- |
| Portal, workers, scheduler, launcher | `portal_web`, `portal_workers`, `ctf_scheduler`, `provisioner_launcher` | RDS IAM from start (`DB_AUTH_MODE=rds-iam`) |
| Migration Job | RDS master | Password, owner duties only |
| Provisioner | `provisioner_lambda` | RDS IAM |
| Guacamole | `guacamole_admin` | Password |
| OpenFGA runtime, admin, migration | `openfga_runtime`, `openfga_admin`, `openfga_migrator` | Rotated password |

Every connection uses `sslmode=verify-full` with the region's RDS CA bundle.
The renderer publishes it as `DB_SSL_ROOT_CERT_PEM_B64`. The entrypoint and the
provisioner write it to a read-only file.

The provisioning Job creates the IAM logins before the identity probe and the
OpenFGA database and base logins. The migration Job runs
`reconcile_aws_database_duties`, which reuses the GCP application and policy
owner reconcilers. Each runtime login joins the `portal_runtime` duty. Passwords
reach the server only as SCRAM-SHA-256 verifiers, because the RDS parameter
group logs every statement.

## Rotation and refresh

`modules/portal/eks/openfga.tf` creates the three datastore secrets and the
in-repo rotation function `lambda/openfga_rotation.py`. Runtime and admin use
the alternating-users strategy; the migrator uses single-user. The schedule is
30 days. The function runs in the EKS private subnets under a bounded role.
Its configuration binds each secret ARN to one base login and names the master
secret. The function refuses a secret that holds any other login, so secret
content cannot direct the master credential at another duty.

Secrets Manager runs `testSecret` when it creates a schedule, so the schedules
exist only when `openfga_rotation_schedule` is true. The deploy sets it after the
provisioning Job has created the logins, and keeps it on later. Teardown builds
the same package as deploy, because Terraform validates the function's code
source while it refreshes before a destroy.

`shared.cloud.policy_credential_refresh` runs weekly. Its IAM role reads only
the three secrets. Its Kubernetes Role allows only `patch` on the three
`openfga-<duty>-datastore` Secrets and the two policy Deployments. The
`restrict-openfga-credential-refresh` admission policy denies any Deployment
change from this identity except the restart annotation.

## Gateway renewal

The Guacamole extension selects its identity with `GUACAMOLE_CONTROL_IDENTITY`.
`AwsGatewayIdentity` uses the AWS SDK for Java STS client and the IRSA
credential chain. The `guacamoleClient` role may mint tokens only for the
gateway audience, with ES384 and at most 900 seconds. The portal accepts the
token only when the role ARN equals `GUACAMOLE_CONTROLLER_ROLE_ARN`.

## IAM and operator access

| Grant | Scope |
| --- | --- |
| Portal, workers, CTF scheduler, provisioner launcher `cognito-idp:AdminGetUser` | The installation user pool only |
| `guacamoleClient` `sts:GetWebIdentityToken` | One audience, ES384, at most 900 seconds |
| `policyCredentialRefresh`, provisioning Job | Read the three datastore secrets |
| Runtime roles | Their own `rds-db:connect` user; no master or Guacamole secret |
| Deploy role trust | Exact deploy Environments, exact protected refs, named operators |
| Deploy role tunnel | `AWS-StartPortForwardingSessionToRemoteHost` on the tagged access node |

The operator access node has no public address, no inbound rule and only the
Session Manager core policy. IAM users that assume the deploy role need MFA and
a source identity equal to their user name.

## Verification

Unit tests cover the verifier, admission, the client, session assurance, the
gateway (Python and Java), database settings, the entrypoint, the provisioning
and reconciliation commands, the rotation function and the refresh Job.
Terraform tests cover the IAM grants, secret readers, rotation settings and the
access node. A released-server PostgreSQL test covers the IAM logins and duties.
These tests do not prove live IAM, token renewal, rotation or tunnel behavior.
