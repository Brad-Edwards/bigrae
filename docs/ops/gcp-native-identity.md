# GCP native identity operations

Native SQL authentication uses the attached GKE workload identity and the
released Cloud SQL Auth Proxy. Applications use their existing PostgreSQL
drivers against a loopback listener; the proxy owns renewal and remote TLS.
Cloud connection, database login and SQL privileges remain separate gates.
Local tests qualify the source arrangement. Live renewal and effective IAM
qualification must precede enabling it on an installation.

## Enable the supported configuration

Set `settings.cloud_sql_iam_auth: true` in the existing GCP installation config.
Keep `dynamic_secret_project_id` explicit. It may equal the platform project,
or name a separate deployment-owned dynamic-secret project. Single-project
creation is parent-scoped and can create arbitrary empty Secret containers;
existing-secret payload and lifecycle permissions remain prefix-conditioned.
No per-customer project or database is required.

Optional `settings.openfga` contains `enabled`, `store_id`, `model_id`, and
`runtime_secret_ref`, `admin_secret_ref`, `ca_secret_ref`. References use
`projects/synthetic-project/secrets/synthetic-name`; the three references must
be distinct. Policy enablement requires native SQL. Store/model IDs are either
both blank for initial publication or both valid published ULIDs.

The runtime and admin Secret Manager JSON bundles each contain only
`server-preshared-keys`, `api-token`, `tls.crt`, `tls.key`. Use separate server
key sets and TLS private keys. The client token must belong to its own server
key set; authentication keys must have at least 32 characters. The CA bundle
contains only `ca.crt`. Certificates must cover the respective private service
names and chain to this CA. Provision these through the established operator
secret workflow; never place values in installation settings, Helm values,
Terraform outputs, command arguments or public evidence.

Deployment reads these named secrets under its existing operator authority and
publishes separate Kubernetes Secrets through stdin using server-side apply.
It maps the runtime bundle's `api-token` to the chart's established
`runtime-preshared-key` field. It does not grant application workers
provider-secret reads for policy keys.

## Owner and publication order

Before upgrading a retained database, use the old release to drain legacy spare
ranges and settle their retained rows. Migration `ctf.0071` refuses to remove
`owner_user` while any legacy spare row remains, including consumed or
soft-deleted rows. It preserves those rows on refusal. A new database has no
legacy spare rows and can proceed.
Migration `ctf.0076` also blocks unfinished legacy event staff or ownership
transitions. Finish them on the previous release before upgrading. The new
release requires exact versioned effect plans and cannot reconstruct an
unfinished legacy declaration safely. Confirmed history is preserved.
Unsupported legacy workspace administrator roles also block cutover. They need
a reviewed policy disposition; the new direct administrator role would add
owner-transfer permission. Cutover inventories memberships in already-bound
organizations as well as organizations that still need conversion.

The deployment database owner runs application migrations, then
`manage.py reconcile_gcp_database_duties`, before policy datastore migrations.
The owner must actually own the application schema and have authority over the
configured SQL roles. A missing ownership/admin-option grant fails closed;
runtime identities do not gain global role administration as a workaround.
The owner reconciles documented Cloud SQL runtime defaults and transfers only
the policy database/schema and released OpenFGA tables to its migration duty.
The policy migration Job runs the released server migrations and PostgreSQL
duty script before the servers become ready.

With blank store/model IDs, the private servers start while the portal policy
client remains unbound. An authorized deployment administrator connects to
`service/openfga-admin` through an approved administrator session (for example
authorized Kubernetes port forwarding). Use the released OpenFGA CLI/SDK with
the admin credential and verified CA to create the store and publish the model
from `manage.py render_openfga_model`. Keep the credential in a protected file
or the SDK's native secret input, never a command argument. Record the returned
store/model IDs in installation settings and redeploy. In-cluster publication
uses the `openfga-publisher` component label and its restricted network lane;
portal/worker identities must not create or relabel that workload.

Rotation synchronizes the named bundles and restarts the runtime server, admin
server and portal. These released processes read credentials at startup.
Qualify overlapping-key and CA rotations with the supported client lifecycle;
mounted-file changes alone do not reload an established client.

## Live acceptance evidence

The shared `deploy.py gcp-deploy` command owns this sequence; CI invokes the
same command. Helm is the sole renderer. For the first cutover, supply both
`--identity-cutover-facts` and `--identity-bootstrap-facts` as owner-only local
file paths. Their values do not belong in the deployment bundle or command line.
Provider account creation alone grants no native application permission.

The identity status probe installs namespace, service-account, configuration,
secret and DNS, metadata-server, Google API and native SQL network prerequisites.
The complete set is required on a fresh namespace before proxy authentication
and SQL connection establishment. The probe does not stop application
workloads. When the probe requires cutover, maintenance validates all workload
inventories before effects. It removes autoscalers, suspends scheduled work,
stops application workloads and waits for their pods to become terminal or
absent. Only then does it apply the stopped release and run migration. Policy
services remain available. When model access is disabled, broker retirement
precedes restoration of broader default egress policy.
An incomplete probe, database failure, or unknown status aborts deployment
before workload changes. An absent legacy cutover table is a known
pre-cutover state. An activated result must also pass the runtime binding check.
The command must not restart policy workloads to turn an inconclusive probe
into a successful one. If a credential rotation makes the incumbent runtime
unverifiable, the probe stops before any workload restart. Restore a verifiable
policy binding before retrying the deployment.

For interrupted application publication, restore the policy service first.
Run `python manage.py reconcile_hierarchy_publications --limit 100` to resume
pending account, organization and workspace edges. For a pending event, run
`python manage.py resume_ctf_event_initialization <event-uuid>`. A trusted operator
may add `--credential-reference '<current-reference-json>'` when the original
credential has expired. Supply only the non-secret reference schema; the command
resolves and checks the current proof and permissions. Add `--abort` to remove
an unusable pending declaration under current parent authority. Aborting refuses
any event with participant or spare allocation. Default event recovery
revalidates the original credential and pinned content. Explicit recovery checks
the supplied current credential and permissions instead of replaying a revoked
proof. Changed content remains blocked. Neither command substitutes a new owner
or grants administrator authority.

The event's **Event staff** card shows an interrupted staff or ownership change.
Use **Retry pending change** to resume its exact recorded declaration, or
**Check status** to refresh its confirmation state. Recovery permits an admitted
current administrator to finish a retained plan after recipient deactivation;
new grants still require an active recipient. If the source administrator revoke already took effect, recovery
reads back that exact admitted change without another provider write. Permanent
event deletion queues native cleanup before removing event records. If cleanup
cannot be queued, the records remain available for retry.

Use synthetic fixtures and retain deployment-specific evidence privately.
Check all of the following before activation:

1. Sustain real cloud/application calls and establish fresh SQL connections
   beyond proof expiry using the actual attached identity and native clients.
   Unavailable renewal must fail closed without disclosing provider errors.
2. Prove intended cloud operations under an explicitly granted operator
   identity and application operations under its separate application grant.
   Inspect inherited IAM/groups, WIF subjects, token minting, `actAs`, and
   cluster-wide namespace/KSA reuse; project-wide pools are not cluster isolation.
3. Prove unrelated workloads, plugins and guests cannot obtain or impersonate
   operator identities, read named policy secrets, reach the admin listener,
   or query application/policy tables outside their SQL duties. Include
   Kubernetes workload/SA/ConfigMap mutation, exec, bind/escalate and token-request
   authority; absence of Secret `get` alone is insufficient.
4. Pair runtime tuple/check success with denied store creation, model publication,
   assertion updates and store deletion. Pair admin publication success with
   runtime-key denial at the admin listener. Check effective additive network
   policy and actual private-IP connector traffic on 3307.
5. Qualify fresh enablement, upgrades, native Job startup/completion, credential
   rotation and disabled-mode behavior. Preserve the coordinated S8 application
   authority cutover.

The runtime client remains a trusted relationship writer. SQL administration
isolation does not provide per-user HTTP authorization or contain a compromised
authorized tuple writer.
