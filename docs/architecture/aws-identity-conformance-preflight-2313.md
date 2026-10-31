# AWS identity conformance preflight — #2313

> Historical preflight, 2026-10-29. This records the inspected source and design
> constraints, not implementation, deployment approval or live qualification.

## Contract and readiness

The supplied #2313 issue is the contract; no formal requirement is attached.
The inspected baseline is `b238189a513a3269dde535bf71e1aaf4ec7b3e03`, the merge
of #2592 / #2321's identity hard cut. Use [ADR-066](../adr/066-account-principal-resource-scope.md),
the current [authorization contract](../technical/shifter_platform/authorization.md),
[credential contract](../technical/shifter_platform/access-credentials.md), and
[identity/lifecycle contracts](../technical/shifter_platform/identity-lifecycle-contracts.md).
Earlier slice proposals do not override the merged implementation.

This baseline establishes the shared architecture. #2308 closed as complete on
2026-10-29, which satisfies the dependency. The owner resolved the native
service-proof and provider-revocation questions below on the same day. The
[AWS integration design](../design/aws-native-identity-integration-2313.md)
records those decisions and the approved deviations. This is not an
implementation plan or a new approval process.

## Decisions carried forward

- Keep one application authorization architecture: OpenFGA, its pinned model,
  action catalog, credential ceilings, deny precedence and durable publication.
  AWS-native authentication and IAM do not justify a second Cedar/Verified
  Permissions policy model or an IAM-to-application-role translation.
- Reuse Cognito/OIDC for human authentication and the existing EKS IRSA/AWS SDK
  boundary for cloud workloads. Select the native application service proof
  explicitly; an IRSA credential alone is not a Shifter application credential.
- Preserve `Account` as the typed customer root, independent human/service
  `Principal`, exact immutable provider binding and SQL-owned resource ancestry.
  An AWS account, Cognito pool/client, IAM role, STS session and Shifter Account
  are different identities. Individuals have no subdivisions. Default business
  hierarchy creation grants nothing. Contacts/creators never lend authority.
- One operator service may receive both cloud administration and complete
  application administration through separate explicit grants. Do not restrict
  supported actions by service kind or AWS deployment; do not broaden ordinary
  workload defaults. Personal tokens remain permission-gated human credentials;
  email-free temporary participants remain event-bound humans.

## Confirmed gaps and decisions that cannot be assumed

Paths below `config`, `management`, `shared`, `workspaces`, `engine`, `ctf` and
`tests` are relative to `shifter/shifter_platform`.

| Inspected gap | Required design boundary |
| --- | --- |
| `config.apps` binds the Google verifier in `config.service_identity`; `management.service_credentials` hardcodes Google's issuer and numeric subject; `management/api/credential_serializers.py` also requires a numeric subject; `config.credential_reference` uses the GCP audience. `shifter/shifter_client/__init__.py` supplies Google ADC renewal. | Provider selection must cover verification, registration/validation, durable-reference resolution and the renewing client together. Reuse `ServiceCredentialAdmission`, `ProviderBinding`, `CredentialContext` and the management facade. Do not patch only the verifier, loosen every subject to arbitrary text, invent service users, or put AWS SDK imports in `shared` contracts. |
| `ApiTokenAuthentication` admits non-personal service proof only as a bounded Bearer JWT. The Terraform Cognito portal client enables authorization-code login, not service credentials. | Document the selected native proof's issuer, immutable subject, intended recipient/client, token use, scopes, expiry, renewal and revocation. IAM SigV4/STS proof is not this JWT contract. Do not wrap STS in a new bearer format or trust forwarded identity headers. Any different transport must explicitly preserve edge authentication and the existing credential ceiling. |
| `config.session_credentials` applies bounded provider assurance only to `IdentityPlatformBackend`; its issuer/tenant and provider-state checks are Firebase-specific. OIDC login already checks issuer/audience and immutable bindings, but does not supply equivalent ongoing assurance through this path. | Extend the existing session lifecycle to the selected Cognito proof semantics across HTTP, Channels and durable references. Preserve local disable, idle/absolute deadlines, bounded provider checks and fail-closed dependency loss. Successful login or `AdminGetUser` enabled state alone does not establish global-sign-out/token-revocation equivalence. |
| `scripts/bootstrap/aws_eks.py:render_aws_values` does not project OpenFGA configuration or the coordinated identity activation lifecycle. AWS runtime inventories do not deliver those bindings. | Reuse the shared chart and identity-cutover commands/state. Deliver private policy service, duties, model/store publication, bootstrap and runtime admission coherently for fresh install and upgrade. Do not bypass `require_identity_runtime_ready`, mark activation from Helm success, or copy a second cutover state machine. |
| Django's RDS backend renews on each new connection, but this Python adapter does not configure the released OpenFGA process. The provisioner currently uses `sslmode=require`; Django defaults to it without a supplied override/CA. | Establish the actual native/maintained connection mechanism for each process and verify server identity, renewal and reconnect after expiry. Encryption alone is not hostname/CA verification. Retain independent application, provisioner, Guacamole and policy runtime/admin/migration SQL duties. Do not export a one-time IAM password into a long-running service or invent a renewal daemon. |

AWS documents that Cognito client-credentials authentication uses a client
secret and issues an access token, without a refresh token; current native
`GetClientToken` also requires the client secret and does not authorize through
IAM. It is a candidate for automatic reacquisition, not evidence of keyless IAM
application authentication. Its exact claims, stable binding, secret custody and
rotation must fit the approved contract before selection. Keep cloud credentials
and application proofs on separate transports even for the same principal.
See [Cognito M2M](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html),
[token endpoint](https://docs.aws.amazon.com/cognito/latest/developerguide/token-endpoint.html)
and [GetClientToken](https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_GetClientToken.html).

Offline signature/expiry checks do not detect Cognito token revocation. Specify
how native provider operations and local lifecycle checks satisfy the existing
revocation bounds, including already-open sessions/sockets and delayed work.
Do not silently substitute token expiry for revocation or copy Firebase's
revocation timestamp assumptions. Any unsupported equivalence is a decision
requiring disclosure and approval, not a reason to build a new token registry.
See [Cognito revocation](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html).

## Cross-cutting gates and canonical incumbents

| Layer | Reuse and required conformance |
| --- | --- |
| Authentication and edge | `config.oidc`, `_oidc_settings`, `session_credentials`, `websocket_auth`, `socket_authority`, `shared.verified_identity`, `shared.api_tokens.authentication`, and management lifecycle/session owners. Verify signature, configured issuer, appropriate audience/client and token use, time bounds, verified human identity and immutable binding. Do not use `VerifiedIdentity`'s human email requirement for service proof. Bad bearer never falls back to cookies; sessions keep CSRF, secure cookies, redirect/origin/CSP controls and rate limits. Bound key retrieval/timeouts and deny unknown configuration; caller-supplied issuer/key endpoints cannot establish trust. |
| Request/schema validation | `shared.api.strict_json.ClosedJSONParser`, `ClosedSerializer`, owning credential/account serializers, `shared.credentials`, `shared.identity_scope`, `shared.authorization`, and `workspaces.services`. Reject unknown/duplicate keys and malformed scalar types; preserve typed principal/ceiling/target contracts and SQL ancestry checks, including individual accounts without organization/workspace. Update the existing API schema/generated frontend contract when an exposed shape changes. No parallel AWS DTO, scope resolver or validation hierarchy. |
| Authorization and persistence | `config.openfga_authorization`, `shared.authorization.{catalog,model,port}`, management models/services and `workspaces/services/_authorization*`. Reuse live credential resolution, delegation checks, exact store/model binding, immutable generations, SQL fences, audit and primary-backed readback. OpenFGA owns effective grants; SQL owns identity, ancestry and operation metadata. Keep denial distinct from unavailability and ambiguous effects pending. No SQL permission mirror, raw tuple endpoint, network I/O inside policy-journal transactions or success based only on a write acknowledgement. Use forward migrations and existing database constraints, not rewrites of historical migrations. |
| Continuing effects and administration | `shared.operation_authority`, `operation_envelope`, `persisted_envelope`; Engine's existing outbox/launch/result services; CTF authority plans; `config.policy_admin_site`; owning API/controller facades. Retain the actual non-secret credential reference and principal attribution, recheck before effects and final notification delivery, fence generation/lease/cancellation, and reconcile through the existing journal. Preserve catalog-wide service/human administration and participant confinement. Legacy Django flags, provider groups, creator fields and email are not application grants. Recovery is an authorized mutation, not a privileged status read. |
| Installation/configuration | `shifter/installation/{schema,loader,settings_aws,bundle_aws,runtime_inventory_aws,publication}.py`, published backend contract, `config._env_manifest`/`env-manifest.json`, bounded `_credential_settings`/`_openfga_settings`, `OpenFgaRuntimeSettings`, `scripts/bootstrap/aws_eks.py`, Terraform outputs and chart values/schema. Closed operator intent and secret references must survive every renderer and consumer with schema/runtime-inventory parity. Publish generated contracts through their existing generator. Keep derived IDs/provider payloads out of operator settings; do not hand-edit generated manifests or allow arbitrary runtime keys. |
| Secrets and OS exposure | `shared.cloud.sensitive_env`, Kubernetes secret/job builders, startup hydration, `platform/terraform/modules/portal/eks/{iam,kms_secrets}.tf` and `scripts/bootstrap/bootstrap_core.run_cmd_secret_stdin`. Use exact-reader secret references or protected files; verify non-root readability and rotation/reload. `_URL`, `_FILE`, `_ARN` and similar pointer suffixes are treated as non-secret: never hide credentials in them. No tokens/client secrets/SQL URLs in argv, query strings, shell tracing, ConfigMaps, Helm history, Terraform outputs, exception text or retained diagnostics. Terraform `sensitive` does not remove values from state. Environment injection is not isolation from the process or node operator. |
| Cloud IAM and host runtime | Existing EKS workload identity maps, exact IRSA namespace/service-account/audience trust, native SDK credential chain, `shared.cloud.aws.task_runner` and provider-neutral `shared.cloud.kubernetes`; shared chart RBAC/admission/network templates and `engine.ecs` forwarding. Keep `AssumeRole`, `PassRole`, workload/SA mutation, exec, secret mount and node/metadata access in the threat model. Check resource/key policies, boundaries, session policies and applicable organization controls, not only role attachments. Preserve intentional operator powers separately. Native SDK renewal must include its upstream proof. IRSA is not node isolation; restrict IMDS and host networking. Guest/plugin execution must not obtain operator, policy-admin or portal identity. |
| Datastore/policy runtime | `config._database_settings`, `config/db_backends/rds_iam`, provisioner `cloud/aws/db_auth.py`, portal RDS/IAM modules, `platform/charts/shifter/templates/openfga.yaml` and `files/openfga-duties.sql`. Bind IAM connect permission to exact database resource/user and preserve PostgreSQL grants, inheritance/default privileges and migration ownership. Retain distinct private OpenFGA runtime/admin listeners, credentials and database logins, pinned model/server/SDK, TLS and bounded timeouts/pools. Preshared keys alone do not enforce per-client privileges; runtime tuple writers remain trusted. Admit only required DNS, STS/Cognito/key-fetch, private SQL/policy and metadata paths; evaluate additive network policies together. |
| Errors/audit/observability | `CredentialUnavailable`/`CredentialRevoked`, existing identity/cloud exceptions, `shared.api.errors`, `shared.errors`, `shared.audit`/`audit_adapter`, `shared.log_sanitize`, `config._logging_config`/`health_checks` and provisioner redaction. Normalize SDK failures to bounded internal reasons and existing opaque request-ID envelopes; never propagate provider bodies or chained secret-bearing exceptions. Sanitization is not secret redaction. Security mutations retain strict transactional audit and original actor; safe health/renewal/dependency counters and CloudTrail supplement it without token/claim payloads or unbounded identity metric labels. |

AWS describes the IRSA/IMDS boundary explicitly in
[IAM roles for service accounts](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html).
The current management-plane launcher is a Kubernetes Job on EKS; legacy
`engine.ecs` naming does not authorize reviving an ECS portal/task deployment.
Guest runtime delivery remains a separate boundary.

## Extension seam, evidence and boundaries

The seam belongs at existing `config` composition and installation provider
configuration: configured proof/verifier and lifecycle strategy, issuer/pool,
client/intended recipient, validated subject form, renewable credential source,
purpose IAM role/resource, SQL auth mode/duty, and policy endpoint/store/model/CA
and credential references. Derive provider identifiers where possible. A second
operator identity or pool/region must change these bindings, not Account,
Principal, action definitions or owning-domain workflows. Keep unsupported modes
explicitly rejected. Do not add a generic authentication plugin framework or
claim cross-partition support merely because the region parser accepts its name.

Behavioral conformance must reuse `tests/integration/authorization`, the released
OpenFGA harness at `shifter/shifter_platform/scripts/run_openfga_integration.sh`,
credential/session/client and PostgreSQL race tests, and
`tests/contracts/test_identity_authorization_surfaces.py` with its generated
surface inventory. Exercise real mounted APIs, admin, sockets and delayed effects;
direct verifier tests cannot prove complete administrative coverage. Include
cross-account ancestry, independent service/contact lifecycle, delegation,
personal-token eligibility, temporary participants, denial precedence, lost
acknowledgements, revocation during work, stale workers and recovery authorization.

AWS-specific evidence must cover wrong pool/client/token use, key rotation,
provider disable/sign-out, missing or expired renewal sources, process restart
and new SQL connections after expiry, plus cloud and application requests beyond
their initial proof lifetimes without manual intervention. Pair intended operator
success with denied unrelated role, namespace, guest/plugin, secret and SQL-duty
access. Reuse installation bundle/publication tests, AWS bootstrap/renderer and
runtime-role parity tests, admission negative tests, existing IAM/RDS/KMS
checkers and IAM drift tooling. Source tests do not prove live IAM or renewal;
any later live qualification needs authorized infrastructure and redacted evidence.

Repository gates remain `.ground-control.yaml`, `.gc/plan-rules.md`, ADR registry,
`.importlinter`, layer checks, `Makefile`/`make policy`, `.pre-commit-config.yaml`
and `.github/workflows/_quality.yml`. Run the full CI-level ADR guard; source
delivery also runs relevant Python/import, Terraform/TFLint, workflow/actionlint,
rendered chart/schema/kube-linter/kubeconform, PostgreSQL and released-server
checks. AWS renders need their own admission/configuration evidence in addition
to the required GCP chart regression. Deliver AWS operator/configuration docs
and documentation-coverage entries with source; do not weaken guards to fit it.

Non-goals: implementing #2313 in this preflight; declaring #2308 approval or
live-cloud acceptance; a separate AWS policy/identity database, custom JWT or
STS exchange, credential broker, refresh daemon, generic workflow framework,
per-customer cloud account/database mandate, provider-group authorization,
reactivating the ADR-068 parked model broker, changing private-pack ownership,
or adding organizational approval ceremony. Account isolation is application
scope, not a claim of dedicated network, database, key or incident boundaries.
