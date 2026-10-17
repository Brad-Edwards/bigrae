# In-box environment-pack bootstrap seed

`manifest.yaml` declares the scenario packs registered into the tenant by
default (an in-box bootstrap seed, not a BigRAE-shipped distribution catalog).

Per ADR-053/ADR-034, the in-box seed is **not** loaded through a privileged
code path. The bootstrap (`cms.scenarios.inbox.register_inbox_packs`, invoked by
the `bootstrap_inbox_catalog` management command) registers each manifest entry
through the same `cms.services.register_pack` service an operator uses via the
API or the `register_pack` CLI. The seed is dogfooded through the
uniform, entitlement-blind ingestion boundary.

This directory holds only the manifest. The pack sources live in the canonical
collection at [`shifter/environment-packs/`](../../../../environment-packs/README.md).
Each manifest `package_ref` (for example `cms/scenarios/inbox_packs/smoke-linux`)
names the pack's runtime destination under the image app root `/app`. The
portal Dockerfile copies the pack there from the collection.

To ship and register a pack by default, make all three of these changes:

1. Add the pack directory to `shifter/environment-packs/`.
2. Add a `COPY --chown=appuser:appgroup environment-packs/<pack> cms/scenarios/inbox_packs/<pack>`
   line to `shifter/shifter_platform/Dockerfile`.
3. Add one manifest entry. The entry shape matches the operator registration
   request (see `manifest.yaml`).

Then move the pack's `shifter/environment-packs/<pack>/**` glob from the
`quality_only` filter to the `portal_image` filter in
`.github/workflows/deploy.yml`. A platform test checks that the Dockerfile
copies and the manifest agree. The `deploy-workflow-plan-scope` ADR guard reads
the Dockerfile copies and fails when the routing does not match.

Packs are validated as foreign input at registration against the
`raes-env-packs` contract, and their advertised digest must match the exact
associated-artifact inventory. Bootstrap retries are service-level no-ops only
for the same immutable identity; manifest or content drift fails visibly. A
missing or malformed declaration fails the deploy bootstrap, and a failure in
any entry rolls back every new registration from that invocation rather than
installing a partial batch.
