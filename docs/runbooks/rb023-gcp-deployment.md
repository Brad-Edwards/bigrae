# RB023: GitHub-less GCP deploy, update, or teardown failed

Applicability: GCP deployments that use the GitHub-less deploy path (ADR-070).
Operator: platform owner. Verified against the repository: 2026-10-29.

The procedures are in the
[GitHub-less GCP deployment guide](../technical/handbook/gcp-github-less-deployment.md).
This runbook covers a failed `gcp-deploy`, `gcp-release-scan`, `gcp-smoke`, or
`gcp-destroy` run. A CI deployment runs the same commands. Its job log shows the
same messages.

## Symptom

A lifecycle command stops with a status other than 0:

| Exit status | Meaning |
| --- | --- |
| 1 | A stage failed. The command printed the reason and removed its lock. |
| 2 | The command arguments are not valid. |
| 3 | The deployment is current, but the advisory smoke failed. The command printed `The post-deploy smoke failed; the deployment itself completed`. |

Before each step, the command prints `Running:` and the redacted command line.
A failed step prints `<tool> <subcommand> failed with exit status <n>`. These
messages identify the stage:

| Stage | Messages |
| --- | --- |
| Inputs | `required tools are missing from PATH`; `deployment inputs must include bigrae.yaml`; `deployment inputs must include terraform.tfvars or terraform.tfvars.json`; `range-images.json role ... must be projects/<project>/global/images/[family/]<name>`; `the deploy tooling revision does not match the release revision`; `the deploy tooling has local changes`; `a release manifest is required`; `settings.release_trust is required to verify release provenance`; `derived ... identity ... is not a valid service account` |
| Lock | `the deployment lock is held by <command> run <run id> (started <time>)` |
| Infrastructure | `settings.operator_bootstrap is required`; `settings.operator_bootstrap.admin_password_secret_ref is required`; `the operator password secret has an empty latest version`; `cannot read the operator password from Secret Manager`; `GCP operator email must use the ... domain`; `terraform apply failed with exit status <n>` |
| Preconditions | `Required GCE range-cell input ... is not set`; `Range guest image not imported`; `Range guest image check failed` |
| Images | `cannot import release image ...`; `imported image ... resolved to a different digest` |
| Scan | `exact-release scan failed; the rollout is blocked`; `failed to store the redacted release verdict` |
| Verify | `no attestation is stored with ...`; `no attestation of ... satisfies the release trust policy` |
| Rollout | `... belongs to another Helm release; refusing to adopt it`; `running workloads do not match the approved release`; `ingress platform-external did not receive a load-balancer address within 10 minutes`; `certificate platform-managed-cert did not appear for ... within 2 minutes`; `certificate platform-managed-cert is not Active for ... after 60 minutes`; `OpenVPN pool servers did not all report healthy within 10 minutes` |
| Identity | `Legacy identity conversion requires both private facts files`; `Identity status probe was inconclusive; workloads remain unchanged`; `kubectl wait` fails on job `identity-cutover-status-*` or `identity-cutover-*` |
| Catalog | Native application activation and readiness succeeded, but catalog initialization requires a current admitted credential. Preserve the active application, complete DNS and TLS, obtain a native credential, and retry with `--catalog-credential-file`. Workload IAM or an actor username is not a substitute. |
| Teardown | `--confirm must equal the deployment name`; `terraform destroy failed after 6 attempts` |

## Investigation

Record the command, the deployment name, the release revision, and the last
`Running:` line. Run the checks through the deployer image of the release, as
the guide describes, or from a clean checkout of the release revision.

1. Check the tools and the bundle. The preflight renders the Terraform values
   and makes no change.

   Read-only command:

   ```bash
   ./scripts/bootstrap/deploy.py preflight --cloud gcp --env <environment> --store gs://<bucket>
   ./scripts/bootstrap/deploy.py gcp-config show --store gs://<bucket>
   ```

2. For a held lock, read the holder. Find out if that command still runs, for
   example in a CI run or in the shell of another operator.

   Read-only command:

   ```bash
   gcloud storage cat gs://<bucket>/locks/deploy.lock
   ```

3. For a Terraform failure, look for a Terraform state lock. A `default.tflock`
   object under the prefix is a lock that a stopped apply can leave.

   Read-only command:

   ```bash
   gcloud storage ls gs://<project>-terraform-state/shifter/<environment>/platform-core/
   ```

4. For an input failure, check the operator password versions and the range
   images. Do not read a secret value.

   Read-only command:

   ```bash
   gcloud secrets versions list <operator-password-secret> --project <project>
   gcloud compute images describe <image> --project <range-project>
   ```

5. For a scan, verify, or running-image failure, read the evidence. The deploy
   identity can read the release-evidence bucket. Use the newest run ID under
   the revision.

   Read-only command:

   ```bash
   gcloud storage ls --impersonate-service-account=<deploy-identity> \
     gs://<project>-release-evidence/release-scans/<revision>/
   gcloud storage ls --impersonate-service-account=<deploy-identity> \
     gs://<project>-release-evidence/deployments/<revision>/
   gcloud storage cat --impersonate-service-account=<deploy-identity> \
     gs://<project>-release-evidence/release-scans/<revision>/<run-id>/<attempt>/verdict.json
   ```

6. For a rollout failure, read the cluster state through Connect Gateway as the
   deploy identity. Use a separate `KUBECONFIG`, and unset both variables when
   you finish.

   Read-only command:

   ```bash
   export CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=<deploy-identity>
   export KUBECONFIG=/tmp/<environment>-check
   gcloud container fleet memberships get-credentials shifter-<environment>-gke \
     --location <region> --project <project>
   helm -n shifter-system status shifter
   helm -n shifter-system history shifter
   kubectl -n shifter-platform get deploy,pods
   kubectl -n shifter-platform rollout status deploy/<deployment> --timeout=60s
   kubectl -n shifter-platform get events --sort-by=.lastTimestamp
   ```

7. For a Helm failure that rolled back, read the container logs from Cloud
   Logging. The atomic rollback deletes the failed pods, so `kubectl logs` has
   nothing to show.

   Read-only command:

   ```bash
   gcloud logging read 'resource.type="k8s_container" AND resource.labels.namespace_name="shifter-platform" AND resource.labels.container_name="<container>"' \
     --project <project> --freshness=1h --limit 100 --format='value(timestamp,textPayload)'
   ```

8. For a running-image failure, list the image of each container. Compare each
   image with the release manifest.

   Read-only command:

   ```bash
   kubectl -n shifter-platform get pods \
     -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].imageID}{"\n"}{end}'
   ```

9. For an ingress or certificate failure, compare the ingress address, the
   certificate status, and the DNS answer.

   Read-only command:

   ```bash
   kubectl -n shifter-platform get ingress platform-external \
     -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
   kubectl -n shifter-platform get managedcertificate platform-managed-cert \
     -o jsonpath='{.spec.domains} {.status.certificateStatus}'
   dig +short <hostname>
   ```

## Resolution

Fix the cause, then run the same command again from the deployer image of the
release. Use the guide for each step that changes the deployment.

- **Inputs:** Use the deployer image of the release
  ([Get and verify the deployer image](../technical/handbook/gcp-github-less-deployment.md#get-and-verify-the-deployer-image)).
  Fix a bundle error at its source and publish the bundle again
  ([Author and publish the deployment input bundle](../technical/handbook/gcp-github-less-deployment.md#author-and-publish-the-deployment-input-bundle)).
  For an identity name that is too long, pass the identity explicitly
  ([Identity options](../technical/handbook/gcp-github-less-deployment.md#identity-options)).
- **Lock:** When the holder still runs, wait for it. When you know that the
  holder stopped, run the next command with `--break-lock`
  ([Deployment lock](../technical/handbook/gcp-github-less-deployment.md#deployment-lock)).
  The command removes the lock with no prompt.
- **Infrastructure:** Fix the operator password secret or the operator email
  ([Create the initial secrets](../technical/handbook/gcp-github-less-deployment.md#create-the-initial-secrets)).
  A failed Terraform apply does not roll back. Fix the cause in the Terraform
  values, publish them, and deploy again
  ([Update and roll back](../technical/handbook/gcp-github-less-deployment.md#update-and-roll-back)).
  Do not remove a Terraform state lock while an apply can still run. When you
  know that the apply stopped, run `terraform force-unlock` with the generation
  number of the lock object as the lock ID, not the ID in the lock info:
  `gcloud storage objects describe gs://<project>-terraform-state/shifter/<environment>/platform-core/default.tflock --format='value(generation)'`.
  A node pool that stays `RUNNING_WITH_ERROR` after a zone stockout fails the
  apply; follow the stockout entry in
  [the GCP tenant standup gotchas](../dev/gcp-tenant-standup-gotchas.md).
- **Preconditions:** Set `settings.range_cells.zone` and import the range
  images with `gcp-images`. For `Range guest image check failed`, the deploy
  identity could not read an image; the message names the reason, for example
  a missing `compute.images.get` permission. For a deliberate platform-first bring-up, deploy
  with `--allow-missing-range-images`.
- **Images:** Make the release packages public
  ([Publish a release](../technical/handbook/gcp-github-less-deployment.md#publish-a-release)),
  or deploy from a mirror with `--release-source`.
- **Scan:** Deploy a release whose images have no HIGH or CRITICAL finding with
  a fix. Read the raw report under `release-scans/`. There is no bypass.
- **Verify:** Check `settings.release_trust` against the repository that
  published the release. Do not deploy an image that fails its provenance
  check.
- **Rollout:** The Helm upgrade is atomic, so a failed upgrade rolls back the
  Helm release. Fix the cause that the events and the rollout status show, and
  deploy again. For a running-image mismatch, find the owner of the unexpected
  image before you deploy again.
- **Identity:** A first install needs the identity facts files
  ([First install](../technical/handbook/gcp-github-less-deployment.md#first-install)).
  The gotchas show how to write them. When a cutover Job fails, read its
  `portal` container log in Cloud Logging
  (`resource.labels.pod_name:"identity-cutover"`). The Job is gone after the
  command stops.
- **Certificate:** Point the DNS A record of the hostname at the ingress
  address, with no proxy. Then deploy again without `--first-install`
  ([First install](../technical/handbook/gcp-github-less-deployment.md#first-install)).
  When the certificate does not appear, check that `deployment.domain` names
  the hostname.
- **Smoke (exit status 3):** The deployment is current. Find the cause in the
  smoke output, then run `gcp-smoke` again
  ([Smoke and exit status](../technical/handbook/gcp-github-less-deployment.md#smoke-and-exit-status)).
- **Teardown:** Pass the exact deployment name to `--confirm`. Use the deployer
  image of the release that the deployment runs. After a failed destroy, run
  `gcp-destroy` again
  ([Tear down](../technical/handbook/gcp-github-less-deployment.md#tear-down)).

To roll back, deploy the deployer image of an earlier verified release.

## Prevention

Deploy only verified releases by digest. Keep the bundle in the config bucket,
not in a repository. Keep the evidence bucket retention lock. Run the preflight
before each change. Run one lifecycle command at a time for each deployment.

Escalate when a release fails its provenance check, when the scan finds a
HIGH or CRITICAL finding with no fixed release, when the state of a lock holder
is unknown, when a Terraform state lock remains after a stopped run, or when a
teardown would remove a resource outside the selected deployment.
