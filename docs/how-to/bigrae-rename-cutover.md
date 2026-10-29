# Rename the product and repository to BigRAE

Issue #2555 owns the rename. The [identifier inventory](../architecture/bigrae-rename-inventory.md)
records source names and compatibility contracts.

## Binding premise

The user directs: **assume no deployments exist**. There is no deployed cloud
trust, application state or running workload to migrate. Foundation inputs,
Terraform applies, cloud deployment discovery and live-workload rollback
rehearsals are not prerequisites for this issue. Repository directories, GitHub
Environment entries and local Terraform metadata are configuration, not evidence
of a deployment. Do not provision infrastructure to manufacture rename evidence.

## Source and artifact verification

1. Build and install the SDK/client wheels in clean local environments. Verify
   old/new imports, CLI/config aliases, webhook signatures and isolated plugin
   producer/consumer combinations. Check that conflicting aliases fail before use.
2. Regenerate current OpenAPI, runtime environment and translation artifacts;
   run their compatibility checks, application tests and native configuration
   linters. Preserve internal module names and intentionally retained protocol
   or source coordinates according to the inventory.
3. Validate future-deployment templates against the renamed repository. Keep
   one exact configured repository identity with its repository/owner IDs,
   purpose, ref, Environment and workflow restrictions. No old/new cloud-trust
   overlap or cloud apply is needed.
4. Test source compatibility and rollback using local immutable build candidates
   and synthetic fixtures. Do not describe this as a live deployment rehearsal.
   Package candidates remain candidates until the existing release process
   publishes them; this issue does not authorize publication to a new service.

## GitHub repository rename

1. Read back repository `1241599684`, owner `56809295`, its current name,
   branch protections/rulesets for `dev` and `main`, required checks,
   Environments and approval rules, Actions permissions, Pages configuration,
   apps/webhooks, runners and repository variables. Inspect secret names only.
   These checks concern GitHub configuration, not cloud deployments.
2. Prepare the implementation PR against `dev`. Rename the same repository to
   `Brad-Edwards/bigrae` within its existing ownership boundary, preserving its
   immutable ID and protections. Keep the old name unused so repository URL
   redirects remain available.
3. Update the local `origin` URL and canonical repository references in
   `.ground-control.yaml`, `.gc/plan-rules.md`, agent instructions, `Makefile`,
   `.cursor/mcp.json`, `mcp/ops`, current docs, OCI source labels, attestation
   verification, Packer evidence expectations and future-deployment templates.
   Update `_quality.yml` and the ADR guard's expression model/tests together.
   Remove any temporary old-name acceptance before delivery readiness.
4. Verify required hosted CI and SonarCloud against the renamed repository.
   Read back the GitHub settings from preparation and test clone, issue and
   release redirects. An absent or skipped required scan is not a passed gate.
   Check Pages configuration separately because its URL is not a repository
   URL redirect; no new hosting destination is authorized by this procedure.

The old Pages path, `https://brad-edwards.github.io/shifter/`, retires at the
2026-10-25 repository cutover. On 2026-10-29 the repository moved to the
`OpenRAE` organization. Use `https://openrae.github.io/bigrae/`.
Repository redirects continue to serve old source, issue and release links.
Do not recreate the old repository to host a Pages redirect: that would remove
the repository redirects.

## Repository and source rollback

Keep the original source revision and local artifact digests. Rehearse old/new
consumer compatibility against those exact candidates. If the GitHub rename
must be reversed, restore the repository name and matching canonical tooling,
then verify its immutable ID, protections, checks and redirects again. Do not
move artifact tags, rewrite historical attestations or copy secret payloads.
There is no workload rollback, state migration or cloud apply in this procedure.

## Ecosystem and ownership boundary

Coordinate the backend name and `x-bigrae:*` aliases with their owning RAES,
environment-pack and LilRAE repositories using synthetic examples. Keep internal
paths and published protocol coordinates explicit rather than changing them as
product copy.

A transfer to `OpenRAE/bigrae` remains a separate maintainer action after the
protected-branch merge. It requires its own destination-owner, organization,
publisher and repository-integration checks. This source change does not perform
that transfer or establish a new registry or hosting destination.

## Completion evidence

Record the reviewed inventory, actual GitHub name/identity readback, compatible
source and build candidates, passing required checks, updated future-deployment
configuration and repository/source rollback evidence. Mark deployment-state
migration and live-workload rehearsal criteria not applicable under the user's
no-deployments decision. Do not infer deployment existence from configuration.
