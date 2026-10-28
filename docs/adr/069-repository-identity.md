# ADR-069: Repository identity comes from context or configuration, not literals

## Status

Accepted 2026-10-26.

## Context

The repository moved from `Brad-Edwards/shifter` to `Brad-Edwards/bigrae`. The
name was a literal in workflows, the ADR guard, Terraform defaults, bootstrap
defaults, operator scripts, and tests. A rename or a fork broke these places,
and some of them failed instead of skipping.

The literals answer different questions:

- **Self-identity.** A workflow refers to the repository that runs it. The
  runtime context already holds this value.
- **Upstream-only jobs.** A job must run only in the upstream repository, for
  example the Sonar scan that uses upstream secrets.
- **Trust anchors.** A cloud trust policy or a deployed validator states which
  repository it trusts. The token or the artifact cannot supply this value.
- **Operator tooling.** A script acts on a repository for a person. A clone can
  have upstream remotes, so the target must be explicit.
- **Upstream references.** Documentation links, the documentation site, the
  citation, package metadata, historical issue links, and provenance records name
  the project.

Established projects guard upstream-only jobs with a literal repository or owner
(for example Node.js, CPython, Django, Homebrew, and rust-analyzer). In a fork
these jobs skip. The GitHub CLI verifies an attestation against a `--repo`,
`--signer-repo`, or `--signer-workflow` value. GitHub Actions OIDC offers
immutable owner and repository IDs that do not change on a rename. The `gh`
default repository (`gh repo set-default`) is the per-clone target for `gh`
commands.

## Decision

1. **Self-identity uses the runtime context.** A workflow or build that refers
   to its own repository uses `github.repository`, `github.repository_owner`,
   `github.repository_id`, `github.server_url`, or the matching `GITHUB_*`
   environment values. This includes attestation verification of artifacts that
   the same repository built, image source labels, and registry namespaces
   derived from the owner.
2. **Upstream-only jobs compare immutable IDs.** A job that must run only in the
   upstream repository compares `github.repository_id` and
   `github.repository_owner_id`, never the name. The IDs equal the values in the
   one identity declaration, `docs/adr/repository-identity.json`. In a fork the
   job skips.
3. **Trust anchors come from deployment configuration.** Cloud trust (AWS IAM
   OIDC and GCP Workload Identity Federation) and deployed validators get the
   trusted repository from deployment configuration: Terraform variables,
   values that bootstrap resolves and the operator confirms, or deployment
   environment values. Trust binds the immutable IDs. Code and modules carry no
   default repository identity, with one narrow exception: the GCP Workload
   Identity trust module (`platform/terraform/gcp/modules/cicd-oidc-identity`
   and its `gcp/global/cicd-oidc` root) keeps a `github_org`/`github_repo`
   default. A mandatory, un-skippable static check (CKV_GCP_125, enforced by
   `scripts/check_tf_gcp_wif_trust`) must resolve the repository name to
   validate the trust grammar, and checkov scans without deployment variables.
   The immutable `github_repository_id`/`github_owner_id` still have no default
   and are the binding anchor, so a fork or rename cannot federate. Real
   deployments set the name through per-environment tfvars; the default is read
   only by the scanner. `purpose_contexts` also has no default. A GCP deployment
   without GitHub federation ([ADR-070](070-github-less-gcp-deployment.md))
   sets `purpose_contexts` to `{}` and both IDs to empty strings explicitly.
   The module accepts empty IDs only without federation, and
   `scripts/check_tf_gcp_wif_trust` rejects a default on these variables. The
   provenance trust anchor of a GCP deployment is `settings.release_trust` in
   its root config: the immutable repository and owner IDs that sign its
   releases. CI passes the IDs of the running repository instead.
4. **Operator tooling uses the `gh` default repository.** A script resolves its
   target from the clone's `gh` default repository (set with
   `gh repo set-default`, read with `gh repo set-default --view`), from an
   explicit argument, or from a `GH_REPO` value. When none is set, the script
   stops with a clear message. A script never uses `gh repo view` to find its target, because
   without a default it selects an upstream remote.
5. **Upstream references may name the project.** Documentation links, the
   documentation site URL, the citation, package metadata, historical issue and
   pull request links, and provenance records may name the upstream project.
6. **The ADR guard enforces the rules.** The `repository-identity-literals`
   check fails when an executable or configuration file contains the declared
   repository name, owner and repository pair, repository or owner ID, or
   registry owner namespace, outside the declaration and the rule 5 references.
   Tests use synthetic identities.

## Consequences

- A fork runs the same workflows. Upstream-only jobs skip in the fork.
- A rename changes the declaration, the `.ground-control.yaml` `github_repo`
  value, and the deployment configuration. Code does not change.
- Each deployment states its trusted repository in its own configuration.
- An operator runs `gh repo set-default` once in each clone that has more than
  one remote.
