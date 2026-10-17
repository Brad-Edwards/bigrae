# Shifter

Self-service agentic cyber-range platform for authorized purple, red and blue
team exercises, with isolated guest workloads and operator-selected telemetry.
Canonical repository: [Brad-Edwards/shifter](https://github.com/Brad-Edwards/shifter).

Start with the [current-state architecture handbook](docs/technical/architecture.md)
for system context, deployment components, authority, lifecycle, data ownership,
package boundaries and supported extensions. Documentation is authored under
`docs/` and published through the single
[MkDocs site](https://brad-edwards.github.io/shifter/) (ADR-038).

## Get started

- [GCP installation and first exercise](docs/technical/handbook/gcp-first-run.md):
  configuration, bootstrap, health checks and the shipped synthetic smoke pack.
- [Full installation setup](docs/technical/dev/setup.md): prerequisites, cloud
  identity, Terraform inputs, DNS, operator bootstrap and deployment flow.
- [Deployment secrets and variables](docs/dev/deploy-secrets.md): the authoritative
  checklist of references and environment-specific configuration.
- [Scenario contracts](docs/technical/handbook/scenario-contract-transition.md):
  CyberScript and ACES history, the current RAES hard cut and rollback constraints.

CyberScript is retired. Current scenario authoring uses the pinned RAES and
RAES environment-pack releases. Install dependencies from each maintained
package's manifest; do not install a local CyberScript package.

## Develop

Use Python 3.12+, [uv](https://github.com/astral-sh/uv), the frontend's declared
Node version and npm, and [pre-commit](https://pre-commit.com/).
From the repository root:

```shell
uv sync --project shifter/shifter_platform
uv sync --project shifter/installation
npm ci --prefix shifter/shifter_platform/frontend
pre-commit install
```

Use [local development setup](docs/technical/dev/local-setup.md) and
[contribution guidance](CONTRIBUTING.md) for the development workflow.
Production control planes use the shared Helm chart on GKE or EKS. Runtime,
cloud and image prerequisites depend on the selected installation bundle.

## Test and document

Run tests for the packages you change, for example:

```shell
make test-platform
make test-provisioner
python3 scripts/adr_guard/adr_guard.py --all --level ci
uv run --with-requirements docs/requirements.txt mkdocs build --strict
```

See [testing](docs/dev/testing.md) and [ADR enforcement](docs/technical/dev/adr-enforcement.md).
CI owns the full quality matrix; local hooks also enforce architecture and
secret hygiene. Architecture-boundary changes include handbook review.

## Support and security

Shifter is maintained on a community, best-effort basis without a response SLA.
Use [SUPPORT.md](SUPPORT.md) for assistance and support limits and
[SECURITY.md](SECURITY.md) for private vulnerability reporting. Run exercises
only in environments you are authorized to control, with human oversight.

The maintainer is [Brad Edwards](https://github.com/Brad-Edwards). The repository
is MIT licensed; [LICENSE](LICENSE) retains the copyright holders' notices.

## Contributing and releases

Read [CONTRIBUTING.md](CONTRIBUTING.md). Feature PRs target `dev` and use
Conventional Commit titles. Release Please owns the version and `CHANGELOG.md`
from the history promoted to `main`; see
[the release workflow](docs/DEVELOPMENT_WORKFLOW.md).

## Citation

```bibtex
@software{shifter,
  author       = {Edwards, Brad},
  title        = {Shifter: Self-Service Agentic Cyber Range Platform},
  year         = {2026},
  license      = {MIT},
  url          = {https://github.com/Brad-Edwards/shifter}
}
```

Include the exact release or commit you used in your publication.
