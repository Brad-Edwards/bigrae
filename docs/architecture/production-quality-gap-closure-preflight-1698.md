# Production quality gap closure preflight (#1698)

Scope: architecture guidance only; assessed 2026-10-25. Issue #1698 is the
delivery contract, with no attached requirement. ADR-004-R24 is the current
quality-ownership rule. The issue and some contract comments still say R23;
R23 now governs credentialed workflow trust and must not be repurposed.

## Decision and evidence boundary

Close actual production-path gaps by reusing effective blocking checks before
adding jobs. Keep `.github/quality-path-filters.yaml` as the sole executable
ownership/routing registry. This note is an assessment, not another inventory
or an implementation plan. ADR-004-R24 already provides the needed architecture;
no new ADR, application abstraction, or permanent exemption is warranted.

The current full CI-level ADR guard reports 23 ownership findings. All four
R24 exception records expired on 2026-10-17. A separate ADR-019-R1 exception
expired on 2026-10-18 and also prevents a green full guard; it is not authority
to expand this issue into unrelated test refactoring. Do not change the clock,
weaken expiry enforcement, or renew exceptions merely to obtain green checks.

For each recorded anchor, distinguish an absent check, missing ownership
wiring, an unreachable job, and a check whose command misses the artifact.
Removal requires evidence of supported-path coverage or actual retirement.
Any deliberately retained gap uses the existing R24 exception schema with an
exact unit/responsibility anchor, rationale, owner, and future expiry. Do not
silently carry retired paths or convert shipped inputs to config/vendor
exclusions. Existing exception prose is historical evidence, not present scope.

## Current reconciliation findings

| Surface | Incumbent and design constraint |
| --- | --- |
| Three Python utilities: `assert_portal_inspection`, `check_rds_pending_modifications`, `handle_sd_replacement` | Their blocking `*-lint` jobs already run Ruff with `S` security rules from each package's `pyproject.toml`. Assess these as security owners before adding duplicate Bandit jobs. Preserve documented rule exceptions; a configured `[tool.bandit]` section alone is not execution evidence. Existing `*-tests` jobs remain test owners. |
| Four MCP packages | `mcp-lint` already runs package `eslint.config.js` with `eslint-plugin-security`. Before crediting security ownership, prove effective findings fail the command: current `npm run lint` invokes `eslint .` without a warning limit. Verify resolved rule severities using the locked dependencies and a synthetic violating input; warnings with exit zero cannot own security. Tighten this incumbent if needed rather than introduce another scanner for the same risk. Preserve the documented object-injection rule dispositions. |
| `mcp_shared:test` | `mcp/shared/tool-schema-dialect.test.js` exists, but the shared manifest has no test script and the derived test matrix omits it. Reuse the existing Node test runner and contract-derived `mcp-tests` matrix. Dependent-package fanout does not prove this direct suite executes; retain shared-change fanout and test package-only changes too. |
| Kubernetes | Blocking `k8s-lint` renders Kustomize roots and enforces `.kube-linter.yaml` security checks. It can own the risks those checks enforce without promoting advisory Checkov. `k8s-schema` and `.github/actions/validate-gcp-manifests` already provide rendered schema/deploy parity. Raw overlay patches are not complete manifests. Do not count `security-k8s`: the guard explicitly rejects that job id as advisory, independently of its workflow flags. |
| Charts | `chart-tests` already renders neutral defaults and all four provider/environment profiles through Helm and `values.schema.json`; it tests pod security, identities, network isolation, image pinning, and rejection cases. Assess its security coverage before duplicating it. Helm lint remains a real distinct lint gap. Reuse the profile set and existing chart suite, not a new chart-values schema or snapshot-only security check. Generic YAML checks cannot parse raw Helm templates. |
| Container inputs | `guacamole-authority-sast` covers Java bytecode only. `guacamole-authority-tests` builds the client and qualifies real protocols, but its native fixtures use a pinned upstream guacd image rather than building `shifter/engine/guacd/Dockerfile`. The recorded `container_images:test` gap is stale as ownership data, yet coverage of the custom daemon remains incomplete. Dockerfiles, both entrypoint scripts, and `shifter/.dockerignore` need artifact-appropriate evidence. Split ownership only where heterogeneous scopes require it; a Java owner cannot certify shell/Docker inputs. |
| Release image security | `_gcp-dev.yml` already scans exact release OCI digests under ADR-004-R23 and `.trivyignore.yaml`. Preserve that release boundary. A deploy-only scan does not establish PR-tier source ownership, and a PR build cannot replace release-digest evidence. Reuse existing scanner policy where applicable; do not add a second release scanner or grant PR jobs registry credentials. |
| Stack smoke | `scripts/stack-smoke/` contains Python helpers as well as shell and configuration. Reuse Ruff/security-rule and ShellCheck conventions as appropriate; shell-only lint leaves Python unchecked. Preserve `stack_smoke.sh` as the real built-image test, including local IdP, synthetic services, bounded readiness, redaction, and cleanup. `run_stack_smoke` defaults false; both caller selection in `deploy.yml` and inner job gating must permit a harness-only change. |
| Guard tooling | The `adr_guard` unit includes `scripts/quality_ownership/**` as well as `scripts/adr_guard/**` and policy documents. Add only missing lint/security enforcement using existing Ruff/Bandit conventions with explicit code scope. The historical claim of local Ruff coverage is stale: current pre-commit does not target these directories. Retain `adr-guard-tests`, `adr-conformance`, and the independent self-check sentinel; prose/config are not Python source merely because they share a unit. |
| CloudFormation | These are vendor connector templates documented as manually deployed infrastructure, not disposable test fixtures. No dedicated blocking owner is declared. Reuse the existing Checkov toolchain for relevant CloudFormation policy and a native validator such as cfn-lint only for a remaining validation gap; Terraform-only `security-iac` does not scan this directory. Do not deploy a stack to validate it. Preserve vendor identifiers/provenance and ADR-004-R14 dispositions; scanner findings need specific treatment, not wholesale template exclusion. |
| Branding assets | The exception calls root `assets/**` deploy-consumed, but current exact-path/name searches find historical design references and its stylesheet README, not an active consumer. Confirm build contexts, COPY rules, static collection, templates and publication before deciding ownership. If shipped, reuse Stylelint for CSS plus relevant file-format/reference validation; Python SAST cannot validate PNG/CSS. If demonstrably reference-only, use a narrow truthful typed exclusion. No fabricated generic asset scanner or whole-tree exclusion to satisfy three labels. |
| Retired historical paths | `cyberscript` has no current quality unit. Its lint/security exception anchors are stale; do not restore the retired package or imports. The exception's old GDC VM-image description also no longer matches the container unit's current paths. Follow the current tracked estate and supported producers, not the old path list. |

One check may own several responsibilities only when it enforces each. A
security assertion inside a test suite may be credited only with its real
test-skip semantics preserved: lint and security enforcement must remain
independent of `inputs.skip_tests`. Do not mark a skippable test job as the sole
security owner without an eligible enforcement route. No-op parsing,
`compileall`, existence checks, or a scanner accepting all findings are not
substitutes for relevant tests or security policy.

## Cross-cutting boundaries and canonical reuse

| Layer | Required contract |
| --- | --- |
| Auth and runner trust | New portable checks belong on GitHub-hosted runners with `contents: read`, without cloud auth, protected environments, write scopes, or self-hosted access. Preserve the existing Sonar secret boundary. No `pull_request_target` execution of submitted code. Local Docker qualification must use synthetic data, bounded ports/lifetimes, and the existing cleanup pattern; no deployment, image push, or private pack checkout. |
| Secrets and data handling | Checks consume tracked source/config and synthetic fixtures, not live tfvars, state, dotenv files, credentials, or tenant secrets. Do not source operator configuration. No secrets in process argv, image ARG/ENV/layers, cache keys, diagnostics, or artifacts. Existing gitleaks/identifier guards remain additive, not stack-specific SAST owners. New remote processing or artifact destinations require approval. |
| Ownership/config parsing | Reuse `scripts/quality_ownership/contract.py`: version 1, closed keys/responsibilities, contained bounded path patterns, deterministic specificity, and `JobRef`/matrix shapes. `classify_paths.py` must reject unknown paths before outputs; preserve NUL-delimited Git input and deletion handling. Reconcile first-party package identities with `scripts/check_layer_imports/layer_imports.yaml`, not a copied package list. |
| Workflow/env shapes | `actionlint` owns syntax; `_guard/_workflow_model*.py` and `checks/_quality_ownership_{model,routing}.py` own semantic interpretation. Preserve fixed classifier output exports, independent `guard_selfcheck`, full-matrix behavior, transitive `needs`, and MCP matrix selection. `deploy.yml` owns outer `quality_relevant`, `quality_only`, literal `skip_tests: false`, smoke input binding and PR Gate. Quality selection must never widen deploy authorization. |
| Artifact validators | Keep policy in package `pyproject.toml`, `package.json`/lockfiles and ESLint/Stylelint config, `.kube-linter.yaml`, chart `values.schema.json`, Terraform `.checkov.yaml`/validation inventory, and release `.trivyignore.yaml`. Render supported profiles before validation, preserving schema, env allowlists and image/identity contracts. Do not relax these schemas or security settings to fit a new check. Terraform skip policy must not silently become CloudFormation/Kubernetes policy. |
| Host/process and supply chain | Treat paths/matrix values as data, use fixed argv and quoted arguments, and emit only reviewed output keys and single-line values. No eval or matrix-supplied commands. Reuse pinned actions, locked package installs and version/checksum-verified tool installation under ADR-037. Missing tools, empty discovery, renderer failure, malformed output and scanner execution failure must fail rather than silently scan nothing. Bound expensive builds/tests with timeouts. |
| Errors, observability and persistence | Reuse `ContractError`/`UnknownPathError`, workflow `_DwShapeError`, `Violation`, and nonzero CLI exits. Report safe path/unit/responsibility/job identifiers and bounded redacted diagnostics. Actions results, existing test reports and PR Gate are sufficient; no application exception hierarchy, HTTP envelope, audit model, telemetry service, database or durable result store. Temporary renders/results stay in runner temp storage unless an established redacted artifact contract applies. |

The guard proves declared ownership and representative routing; it does not
inspect scanner rule severities or prove every command covers every matched
file. Its union-of-matching-units model can make a heterogeneous unit look
complete despite a language-specific blind spot. Review actual command scope
and prove failure with synthetic policy violations. Extend existing negative
fixtures in `scripts/adr_guard/tests/test_quality_path_ownership.py` and
`test_deploy_workflow.py` where wiring changes need evidence; do not add another
workflow parser, schema, violation family, or registry.

## Extensibility, verification and limits

The extension seam remains a quality-unit record with existing `{job, matrix}`
selectors and reviewed job parameters. Reuse MCP fanout and the chart profile
set; parameterize the existing render/check boundary if another profile or
input family needs it. Do not introduce a generic CI DSL or hard-coded package
lists in several consumers. The current matrix verifier specifically knows
`mcp-lint` and `mcp-tests`; a new matrix family needs semantic reachability and
output-wiring coverage in that incumbent, not an unverified selector.

Implementation evidence must distinguish ordinary docs-only, guardrail,
mixed, unit-only, shared-dependency, deleted/renamed and full-matrix changes.
Preserve negative checks for unknown paths, advisory/soft-fail owners,
miswired outputs, wrong matrix members and self-bypass. Demonstrate actual
failure propagation through the caller and PR Gate; a reachable job or an
uploaded CodeQL/Sonar/Trivy report alone is not a blocking policy verdict.
Do not claim the separate CodeQL workflow satisfies an `_quality.yml` owner.

Required delivery checks remain the full CI-level ADR guard, affected native
checks and successful affected Actions jobs. Workflow edits require actionlint;
Kubernetes/Helm changes also need their existing render, security and schema
checks. Keep enforcement docs/ADR records synchronized when executable policy
changes. Local preflight inspection is not evidence that remote jobs passed.

Non-goals: issue implementation in this preflight; application controllers,
DTOs, services, repositories, runtime authorization or persistence changes;
coverage-floor redesign; new workflow engines or scanner services; broad
legacy-debt cleanup; changing deployment/release trust; private scenario checks
or evidence; requirement creation; publishing or dispatching workflows. Private
pack assets and their qualification remain in their owning repositories.
