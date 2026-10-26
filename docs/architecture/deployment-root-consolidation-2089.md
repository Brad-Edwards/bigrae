# Shared deployment configuration

## Architecture

The GCP platform uses one Terraform execution root.
Each environment supplies its own values from the existing environment directory.
The root keeps the existing module names and provider versions.
Each environment keeps its existing GCS bucket and `shifter/<environment>/platform-core` prefix.
The directory change needs no resource import, state move, or resource replacement.
Terraform metadata remains separate for each environment through `TF_DATA_DIR`.

Deploy, destroy, validation, and local bootstrap use this root.
Local bootstrap reads the selected environment's security settings before cloud operations.
Generated and operator values retain their precedence over committed examples.
One helper selects HCL and JSON var-files in Terraform order for execution and security checks.
Malformed JSON security inputs fail before cloud operations.
Kustomize components hold shared runtime patches.
Environment overlays contain values, generated inputs, image references, and workload identities.
The existing output renderers supply real deployment identities.

The AWS production roots remain available for local deployment and maintenance.
Their owner is @Brad-Edwards.
Their caller is `scripts/bootstrap/deploy.py terraform --env prod --profile <profile>`.
The portal CTFd module also remains available under that owner.
Its caller is the portal root's optional `module.ctfd`.
Its bootstrap uses upstream CTFd setup without a removed seed script.
Unused guest build assets, host scripts, and connector templates leave the supported deployment path.
The standalone workshop root and retired AWS profile are removed after operator confirmation that their resources are drained.
The prior release retains its teardown code for recovery.

## Security and data flows

Protected assets include Terraform state, deployment credentials, runtime secrets, and existing cloud resources.
An incorrect environment selection can expose secrets or change another deployment.
Separate backend metadata and explicit environment values reduce that risk.
Existing GitHub Environment approvals, purpose identities, confirmation checks, and state locks remain in effect.
Tests exercise environment selection and verify that credentials remain isolated.

```text
Operator or protected workflow
  | selected environment, private configuration
  v
Bootstrap or deployment job [execution boundary]
  | environment values + generated values
  v
Shared Terraform root
  | authenticated state reads and locked writes
  v
Environment GCS state [cloud boundary]
  | resource addresses and outputs
  v
Cloud provider APIs and existing output renderers
  | runtime settings, workload identities, image digests
  v
Environment overlay / Helm release [cluster boundary]
  | manifests and secret references
  v
Kubernetes API and workloads
```

State and generated files stay in their established private locations.
Repository examples contain synthetic values.
Public review records contain no private acceptance evidence.
This change adds no service, credential, or runtime permission.
The operator-confirmed GitHub rename changes canonical repository routing to `Brad-Edwards/bigrae`.
The repository connection must restart to capture that origin.
The Sonar admission and artifact provenance checks use the same canonical repository.
Bootstrap defaults and foundation trust inputs use that repository.
Repository trust and scanner admission retain the immutable owner and repository IDs from the integration branch.
Reusable workflow trust retains its exact repository, workflow path and immutable revision checks.
Cloud drift remains possible and requires a live plan for each environment.

## Policy and verification

The Terraform validation inventory owns the shared root and its existing provider lock.
Existing ADR, workflow, Terraform, and Kubernetes checks continue to cover retained surfaces.
Checks specific to removed connector templates retire with those templates.
The ADR enforcement documentation records this change.
Stale connector exceptions and ADR evidence references retire with their source files.
Stale workshop exceptions retire with the drained root.
No architecture rule is weakened.
Existing runtime architecture and trust boundaries need no additional diagram.
The data flow above identifies the configuration and state boundaries affected here.

Verification compares module addresses, provider locks, environment inputs, and rendered manifests.
There is no live application cutover for the currently undeployed environments.
An empty-state plan creates resources and cannot supply evidence of a no-change migration.
Any retained infrastructure state requires a no-change plan before reuse of that environment.
Cleanup uses the last retained revision and the original backend under operator control.
Deletion of a source file never authorizes deletion of a cloud resource.

## References and document review

- HashiCorp Terraform 1.16: GCS backend, configuration directories, and `TF_DATA_DIR`.
- Google Cloud: infrastructure-as-code reference architecture; separate review from application of changes.
- Kubernetes Kustomize: reusable components and environment overlays.
- ASD-STE100 Issue 9: technical terms, short sentences, and one instruction per sentence.

Review checks sentence length, consistent technical names, and document purpose.
Implementation tasks and detailed test commands belong in the issue plan.
Operator cutover and recovery commands belong in the deployment runbook.
