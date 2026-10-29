# Testing: Clean-Checkout Commands and Quality Metrics

This document is the contract for running the test suites and for the coverage
and warning policy that makes the reported numbers trustworthy (REV1 Q6/Q7,
issue #1529). CI (`.github/workflows/_quality.yml`) is the enforcement authority;
the commands here reproduce its posture from a clean checkout.

## Command matrix

This matrix is the single list of verification commands. CI is the enforcement
authority. Unless a row names another workflow, the CI job is in
`.github/workflows/_quality.yml`. The `paths` job selects jobs for the changed
paths, so a pull request runs only the jobs that own its changes.

Run the lanes for the packages you change. `make test` runs every row marked
**yes** in the `make test` column. Do not also run those child targets, because
that runs the same suites twice. Each `make` target syncs pinned dependencies
(`uv sync` or `npm ci`) and sets the CI posture. `make help` lists all targets.

Base prerequisites: `uv` and Node 20.19+ with npm. The prerequisites column lists
only additional tools or services.

### Unit and package lanes

| Lane | Local command | Prerequisites | CI job | `make test` |
| --- | --- | --- | --- | --- |
| Platform (SQLite, local only) | `make test-platform` | None | None; CI runs the PostgreSQL lane | yes |
| Adapter SDK and packaging | `make test-adapter-sdk` | None | Tests (adapter SDK) | yes |
| Engine provisioner | `make test-provisioner` | None | Tests (provisioner) | yes |
| Packer helpers | `make test-packer` | None; Packer cases skip without Packer | Tests (packer) | yes |
| Installation CLI | `make test-installation` | None | Tests (installation) | yes |
| Bootstrap scripts | `make test-bootstrap` | None (unit selection) | Tests (bootstrap) | yes |
| Layer-import checker | `make test-check-layer-imports` | None | Tests (check_layer_imports) | yes |
| Helm chart contracts | `make test-charts` | Helm | Tests (charts) | yes |
| Repository guard | `make test-adr-guard` | None | Tests (adr_guard) | yes |
| GCP scripts | `uv run pytest tests/` in `scripts/gcp` | None (unit selection) | Tests (gcp scripts) | no |
| OpenVPN | `uv run pytest tests/` in `shifter/engine/openvpn` | None | Tests (openvpn) | no |
| Ops helper scripts | `uv run pytest tests/` in each `scripts/<helper>` | None | Tests (`<helper>`) | no |
| Stack-smoke helpers | `python3 -m unittest discover -s scripts/stack-smoke -p 'test_*.py'` | None | Lint and security (stack smoke) | no |
| UAT harness units | `uv run pytest tests/ -q` in each `uat/<harness>` | None | Tests (`<harness>`) | no |
| Terraform contracts | `terraform init -backend=false`, then `terraform test`, in the suite directory | Terraform | Contract test (`<suite>`) | no |

The ops helpers are `handle_sd_replacement`, `check_rds_pending_modifications`
and `assert_portal_inspection`. The UAT harnesses are `event-load-harness` and
`range-functional-smoke`. Their unit tests do not contact a deployment.

CI also runs the `integration` marker for bootstrap and GCP scripts. The local
commands select only unit tests for those packages. See
[external tool integration tests](#external-tool-integration-tests).

### Service and image lanes

| Lane | Local command | Prerequisites | CI job |
| --- | --- | --- | --- |
| Platform (PostgreSQL, coverage floor) | `make test-platform-postgres` | PostgreSQL on `:5432` | Tests (shifter_platform, PostgreSQL `n`/6), Coverage (shifter_platform) |
| Platform Redis channel layer | `make test-platform-redis` | Redis on `:6379` | Tests (shifter_platform, Redis channel layer) |
| OpenFGA authorization | `./scripts/run_openfga_integration.sh` in `shifter/shifter_platform` | Docker | Tests (OpenFGA 1.20.0, PostgreSQL) |
| Subnet migration proof | `uv run pytest engine/tests/test_subnet_allocation_migrations_postgres.py` in `shifter/shifter_platform` | Docker | Tests (migration proof) |
| Guacamole authority | `mvn verify` in `shifter/engine/guacamole/authority-extension`, then the CI job's integration tests | Maven, Docker | Tests and compilation (Guacamole authority) |
| guacd image | The CI job's image build, then `bash shifter/engine/guacamole/test-entrypoint.sh` | Docker | Tests (guacd image) |
| Built-image stack smoke | `bash scripts/stack-smoke/stack_smoke.sh` | Docker | Stack smoke (built image) |
| GCP deployer image | The CI job's fixture release manifest and image build, then `docker run --rm bigrae-deployer:ci gcp-deploy --help` and its Trivy scan | Docker | Tests (GCP deployer image) |

The PostgreSQL and OpenFGA lanes run some of the same tests. They are not
duplicates: each lane proves database or authorization behavior that the other
lane cannot prove. The SQLite lane is a fast local lane; CI does not run it.

### Frontend and browser lanes

| Lane | Local command | Prerequisites | CI job | `make test` |
| --- | --- | --- | --- | --- |
| Legacy platform JavaScript (Jest) | `make test-js` | None | Tests JavaScript (shifter_platform) | yes |
| React SPA lint, types, Vitest, build | `make test-spa` | None | SPA (shifter_platform frontend) | yes |
| API contract and generated client | The command below, in `shifter/shifter_platform` | None | API contract (shifter_platform) | no |
| Authenticated SPA journeys | `make test-platform-e2e` | Chromium (the target installs it) | SPA E2E (shifter_platform frontend) | no |
| Browser accessibility | `make test-platform-a11y` | Chromium (the target installs it) | Accessibility (`deploy.yml`) | no |

The API contract check needs the test posture, as in CI. The secret key is a
synthetic, non-secret value:

```bash
TESTING=1 TEST_DB_BACKEND=sqlite DJANGO_DEBUG=true \
  DJANGO_SECRET_KEY=shifter-local-make-test-key \
  uv run python manage.py api_contract --check
```

The two browser targets build the SPA and use SQLite. CI uses PostgreSQL. Each
target deletes `shifter/shifter_platform/db.sqlite3`. Do not run them at the same
time or against an active development server. Both lanes fail on a Content
Security Policy violation. See [browser security policy](#browser-security-policy).

### MCP lanes

| Lane | Local command | Prerequisites | CI job | `make test` |
| --- | --- | --- | --- | --- |
| MCP server packages | `make test-mcp` | None | Lint JavaScript (mcp/`<package>`), Tests JavaScript (mcp/`<package>`) | yes |

`make test-mcp` runs `npm ci`, `npm run lint` and `npm test` in each package
under `mcp/`. Lint allows zero warnings.

### Policy and static checks

| Check | Local command | CI job |
| --- | --- | --- |
| Architecture guard, imports, whitespace, changed prose | `make policy` | Architecture (adr conformance), Architecture (import linter), Docs prose (changed Markdown) in `vale.yml` |
| Commit hooks | `pre-commit run` | None; the hooks run locally before each commit |
| Workflows, Terraform, Kubernetes | The native commands in [AGENTS.md](https://github.com/Brad-Edwards/bigrae/blob/dev/AGENTS.md#required-checks) | Lint (GitHub Actions), Lint (terraform), Lint and security (Helm chart), Lint (k8s schemas) |

`make policy` runs the ADR guard once. Do not also run the configured lint
command (`adr_guard.py --all --level ci`) in the same pass. Vale checks only the
committed Markdown changes against `origin/dev`.

### Live qualification

These checks run against a deployed tenant. They need operator authority and are
not pull-request gates. A pass proves only the scope that its runbook states.

| Check | Command or entrypoint | Runbook or status |
| --- | --- | --- |
| Post-deploy range smoke | `manage.py run_post_deploy_smoke`, run by the deploy workflows | Advisory: a failure opens an issue and does not block the deploy |
| Participant terminal and Guacamole session | `uat/range-functional-smoke` | [Range functional smoke](https://github.com/Brad-Edwards/bigrae/blob/dev/uat/range-functional-smoke/README.md) |
| Event load envelope | `event-load-harness` | [GCP event capacity](../ops/gcp-event-capacity.md) |
| Range escape boundaries | `manage.py run_range_escape_validation` | [Range escape validation](../ops/range-escape-validation.md) |
| Model broker transport | `scripts/gcp/probe_model_broker.py` | [Model broker probes](../ops/model-access-gcp-probes.md) |
| Range audit telemetry | `scripts/gcp/verify_range_audit_telemetry.py` | [Range audit telemetry](../ops/gcp-range-audit-telemetry.md) |

Range readiness or an open port does not prove a working terminal session. An
accepted destroy request does not prove that teardown completed.

You can also run a package directly (`cd <pkg> && uv run pytest`) from a clean
checkout. The settings set the platform test posture (`DEBUG`, SQLite backend,
local provisioner off) through `config._runtime_env.IS_TEST_RUN`; CI does not
inject it.

## CI platform lane

CI runs the platform suite one time, against PostgreSQL. `pytest-split` divides
the suite into six parallel shards. It uses the `least_duration` algorithm with
no durations file, so it deals the tests out to the shards in turn. Each xdist
worker clones its test database from one migrated template database. The
`Coverage (shifter_platform)` job then does these steps:

1. It combines the coverage data from all shards.
2. It enforces the whole-package coverage floor.
3. It makes sure that at least one `postgres`-marked test ran in a shard
   (`scripts/check_postgres_lane_evidence.py`).

A skipped `postgres`-marked test fails its shard. Keep each test fast. One slow
test makes its shard slow, because the shards do not use recorded durations.

## Test posture (why a clean run matches CI)

`config._runtime_env.IS_TEST_RUN` is true when `TESTING=1` or pytest is the entry
point. Under it the settings default to the test posture so a bare `uv run pytest`
does not inherit the production HTTPS posture a fresh checkout would otherwise get:

- `DEBUG` defaults to `True` (`config/settings.py`). An explicit `DJANGO_DEBUG`
  still wins; production (`IS_TEST_RUN` false) is unchanged and defaults to `False`.
- `LOCAL_PROVISIONER` is forced off (`config/_cloud.py`) so range-lifecycle tests
  never shell out to a real provisioner subprocess (which leaks). A test that
  exercises that path sets `settings.LOCAL_PROVISIONER` via the pytest-django
  `settings` fixture.
- `TEST_DB_BACKEND` defaults to `sqlite` (`config/_database_settings.py`).

These are covered by `tests/platform/test_clean_checkout_posture.py`.

## Coverage: two complementary floors

Production coverage measures owned code only. Each publishing package sets
`[tool.coverage.run] source` and omits the whole test tree, conftest hooks, and
migrations in its `pyproject.toml`, so tests can no longer cover themselves (the
platform metric was 94% self-covering, ~88% production-only). CI runs `pytest
--cov` (no path) so package config, not the command line, owns the measured set.

**Absolute per-package floor**, enforced by `[tool.coverage.report] fail_under`
in each package. Floors are per-package (a repository aggregate would let a
high-coverage package mask a regression in a smaller one). Each floor sits one
point under the measured production-only baseline to absorb run-to-run variance;
floors only stay level or rise, and a real reduction requires a reviewed,
time-bounded exception.

| Package | Production-only baseline | `fail_under` |
| --- | --- | --- |
| `shifter/shifter_platform` | 88% | 87 |
| `shifter/engine/provisioner` | 80% | 79 |
| `shifter/installation` | 97% | 96 |
| `scripts/bootstrap` | 86% | 85 |
| `scripts/check_layer_imports` | 96% | 95 |
| `shifter/packer` | 76% | 75 |

**SPA (frontend) floor.** The Django-hosted React SPA
(`shifter/shifter_platform/frontend`) enforces its own absolute floors in
`vite.config.ts` (`test.coverage.thresholds` for statements, branches,
functions, and lines), each set one point under the measured baseline like the
Python packages so the floor absorbs run-to-run variance and only rises. The
measured source set is `src/**/*.{ts,tsx}` excluding tests, test support, and
the generated `src/api/schema.d.ts`; the first-party entrypoint and composition
root stay measured. The SPA's Vitest LCOV (`frontend/coverage/lcov.info`) is
uploaded by the `SPA (shifter_platform frontend)` CI job and restored into the
`sonarcloud` job, so SPA changed lines are held to the same changed-code gate
below (#1526).

**Changed-code floor**, owned by the SonarCloud `raes-strict` quality gate,
which fails a PR when `new_coverage < 80` (80% coverage on changed lines), plus
`new_violations > 0`, new duplicated-lines density > 3%, any new rating worse than
A, and new security hotspots not fully reviewed. PR analysis waits for the gate
(`sonar.qualitygate.wait=true`). The conditions are recorded in
`sonar-project.properties`; each owned Python package publishes its configured
coverage report for Sonar to consume.

The `sonar-rules-changed-lines` pre-commit hook finds most `new_violations`
before a push. It applies the Python rules that fail the gate most often to the
lines that changed against `origin/dev`. It does not run tests, so it does not
check `new_coverage`.

The released OpenFGA/PostgreSQL lane also publishes `coverage-openfga.xml`,
including the native application and policy SQL duty tests. Sonar consumes both
platform reports so service-only behavior contributes real coverage evidence.
This supplemental report has no whole-package floor because it exercises a
service-specific subset; the `Coverage (shifter_platform)` job still enforces the
unchanged 87% package floor, and Sonar still enforces 80% changed-code coverage. Run it locally
from `shifter/shifter_platform` with
`./scripts/run_openfga_integration.sh --cov --cov-report=xml:coverage-openfga.xml --cov-fail-under=0`.

## Warning policy

Warning classification lives in `shifter/shifter_platform/pyproject.toml`
`[tool.pytest.ini_options] filterwarnings`, so a direct `pytest` and CI agree
regardless of test order or xdist worker.

- **`ResourceWarning` and unawaited-coroutine `RuntimeWarning` are errors.** They
  are correctness defects; fix the originating fixture/mock (close/await the
  resource) rather than downgrade the filter. Converting to `ignore`, adding a
  sleep, forcing GC, or disabling xdist is not a fix.
- **`DeprecationWarning` is an error by default.** First-party deprecations, and
  any new or changed third-party deprecation, fail until fixed or explicitly
  baselined. Do not add a blanket `ignore::DeprecationWarning`.
- **Third-party deprecation allowances are narrow and owned.** Each is an
  `ignore` filter matched by exact message, with an adjacent owner and removal
  condition. The current set is the `django-health-check` (`<5.0`) class-based
  check deprecations, tracked for migration + removal in
  [#1601](https://github.com/Brad-Edwards/bigrae/issues/1601).
- Tests that intentionally assert a warning use `pytest.warns` at the behavior
  boundary; that is an assertion, not a baseline exception.

## Authenticated SPA end-to-end (Playwright)

`make test-platform-e2e` runs the authenticated browser journeys against the
**real Django-hosted built SPA** (#1526). It is hermetic: it builds the SPA,
migrates and seeds a job-local database, and drives Playwright through normal
Django sessions and CSRF. The CI job `shifter-platform-e2e` in
`.github/workflows/_quality.yml` runs the same flow against a job-local
PostgreSQL service and feeds the `PR Gate`; the make target uses SQLite locally.

Synthetic actors are established by the dev/test-only `seed_e2e` management
command (staff, threat-research, and standard actors, plus the staff actor's
administrable organization) and by `dev_login` (CTF organizer/participant). The
dev/test-only `seed_e2e_ctf` command adds one CTF event with inbox messages for
the inbox participant actor. The
Playwright config's `auth.setup.ts` logs each actor in once and reuses its
`storageState`. No cloud, provisioner, IdP, or live-range dependency is used:
`LOCAL_PROVISIONER`/`ENGINE_TASK_*` stay unset, so a range launch enqueues
nothing and contacts no cloud.

Hermetic scope: login/auth-revocation, workspace lifecycle
(create/rename/archive/restore), shared-audit history, authorization denial
(client advisory **and** the authoritative API 403), CTF
organizer/participant/threat-research role landings, and the participant inbox
with stored and reflected XSS probes under the enforced CSP. Journeys that require a live
provisioner (range readiness/terminal/cleanup, CTF range, Guacamole) are the
deployed tier and are tracked separately, not run in the PR lane.

Authenticated Playwright traces are retained only on a local retry and are never
uploaded as CI artifacts (ADR-055-R7). Reproduce failures locally with
`make test-platform-e2e`.

## Browser accessibility (Playwright + axe)

`make test-platform-a11y` runs `@axe-core/playwright` over the ADR-055 surface
matrix (`frontend/e2e/a11y/`) against the same hermetic stack, with the WCAG 2.2
A/AA tag set. The `Accessibility` job in `.github/workflows/deploy.yml` runs it on
**every** pull request as a direct `PR Gate` dependency (it does not inherit the
frontend path skip, per ADR-055-R2), against a job-local PostgreSQL.

The debt baseline is an exact set of privacy-safe fingerprints
(`frontend/e2e/a11y/baseline.json`): the scan fails on any new or resolved
finding, and the `accessibility-baseline` adr_guard check enforces that the
committed baseline may only shrink versus the trusted base branch (additions
require an exact-fingerprint waiver in `docs/adr/exceptions.yaml` naming ADR-055,
per R4/R6). The matrix reconciles fail-closed against the SPA route table
(`src/test/a11y-surface-reconcile.test.ts`); a new route group with no coverage
or exclusion fails. Nightly multi-browser runs, deployed-target scans, the
WCAG-EM manual audit, and the release-evidence gate are the remaining ADR-055
tail tracked in [#2117](https://github.com/Brad-Edwards/bigrae/issues/2117).

## Browser security policy

The portal enforces the ADR-036 Content Security Policy. The browser lanes record
each `securitypolicyviolation` event through `frontend/e2e/support/csp.ts`. A
journey, accessibility scan or live Django page fails on any violation.

`e2e/journeys/csp.spec.ts` checks the enforced header and blocked inline script
and handlers. It also checks that same-origin WebSockets connect and off-origin
connections are blocked. `e2e/journeys/django-pages.spec.ts` covers the live
Django-rendered pages. These tests use Chromium only. The deployed identity
provider, MFA and range terminal paths are outside the hermetic lanes; #2117
owns that tier. [RB024](../runbooks/rb024-browser-csp-violation.md) is the
operator procedure for a blocked feature.

## External tool integration tests

The GCP scripts, bootstrap, adapter SDK and Packer packages register the
`integration` pytest marker for checks that require external rendering/build
commands or isolated package/TLS environments. Use `uv run pytest -m "not integration"`
from a package directory for its tool-independent unit selection, or
`uv run pytest -m integration` to run the external-tool checks explicitly.
GCP scripts and bootstrap default to the unit selection; their CI jobs explicitly
select both with `-m "integration or not integration"`. SDK and Packer default
runs retain both selections. No regression checks are removed from CI.

The bootstrap and GCP script CI jobs install the same digest-verified Helm build;
the GCP script job also installs the pinned kubectl client. Bootstrap verifies its
OpenSSL prerequisite. SDK wheel conformance requires uv and the runtime extra's
cached dependencies; guest TLS integration requires OpenSSL. Packer validation
requires Packer and its plugins. These checks do not deploy cloud resources.

`shifter/packer/tests/test_scripts.sh` and the `TestScriptStructureLint` and
`TestScriptContentLint` classes are lint-only checks. Their success does not prove
provisioning behavior or qualify an image. The script execution suites use
controlled command shims; built-image behavior still requires image qualification.
