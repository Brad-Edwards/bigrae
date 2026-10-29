# Built-image stack smoke (#922)

`stack_smoke.sh` boots the **production portal image** under its **real
`entrypoint.sh`** against local Postgres / Redis / ElasticMQ doubles and released
OpenFGA, and asserts the runtime contracts the source-tree pytest estate cannot
see. It is the same
class of regression the June-7 hotfix wave shipped live (portal home-directory,
worker container healthchecks): failures that only appear in the built artifact
under its real entrypoint, never in `pytest` against the source tree with test
settings.

It runs identically locally and on a hosted CI runner, with **no cloud
credentials**: the only AWS surface is a local ElasticMQ SQS double reached via
`AWS_ENDPOINT_URL` with dummy creds.

## What it asserts

1. **Image build**: the production `Dockerfile` (context `./shifter`) installs
   deps and runs `compilemessages` / `collectstatic` as the non-root `appuser`.
2. **Native activation**: after migrations, the harness runs the released
   OpenFGA datastore migrations, applies the chart's separate admin/runtime SQL
   duties, starts TLS servers with distinct tokens, and publishes the built
   image's authorization model through the admin API. A normal image command
   must fail the real entrypoint gate before activation. A bounded maintenance
   one-shot creates a distinct synthetic human operator and native provider binding through
   Management services, then calls the real `identity_cutover plan`, `apply`,
   `identity_cutover_bootstrap`, `verify`, and `identity_runtime_check` commands.
   The trusted cutover facts explicitly opt in to first-principal personal-token
   eligibility with `bootstrap_personal_token_eligibility: true`, retained in
   the plan digest. Bootstrap writes and verifies that separate grant; the
   administrator role alone does not enable token use. No ordinary self-grant
   is attempted.
   It never inserts activated state or replaces the authorization provider.
3. **Boot**: `entrypoint.sh` waits for the DB, runs migrations exactly once
   (a dedicated one-shot; every long-running container boots with
   `SKIP_MIGRATIONS=1`, matching the shared chart’s separate migration Job), and
   execs the production Gunicorn/Uvicorn ASGI command as `appuser`.
4. **Readiness**: `/health/` returns 200 from the real dependency-aware
   `django-health-check` registry (DB + cache + storage + Redis channel layer).
5. **Native catalog setup**: after web readiness, a real OIDC operator login
   establishes the session used by `issue_personal_token`. The normal owning
   service issues a real Knox token with only installation content read/manage/
   publish scopes, checking the released policy service. The private bearer file
   goes to `bootstrap_inbox_catalog --credential-file`; the resulting shipped
   sources must have that exact native owner, installation custody, conformance
   receipts and explicit publication. No staff flag or implicit deployment actor
   authorizes registration.
6. **Real OIDC first login** (`#988`): a real authorization-code flow against a local
   Cognito-shaped provider double drives `/login/` then `/oauth2/authorize` then
   `/oidc/callback/` through `mozilla_django_oidc` + `ShifterOIDCBackend`,
   provisions the first-login Django user, human Principal, exact ProviderBinding
   and PrincipalSession (proved absent before the flow, then bound to the verified
   `(issuer, subject)` after), and establishes the Django session reused by the
   checks below. A regression in OIDC config, the callback, first-login
   provisioning, or session establishment fails the smoke.
7. **Websocket**: an authenticated handshake completes through
   `AllowedHostsOriginValidator`, `AuthMiddlewareStack`, and a routed consumer
   (`ws/notifications/`), using the session established by the login flow above.
8. **Authenticated page renders**: the real dashboard / range / terminal /
   settings / help pages return 200 off the built image, and every local
   `/static/` asset they reference (plus any declared sourcemap) resolves,
   catching the missing-terminal-sourcemaps / static-asset regression class
   (`#923` TEST-3, range-independent subset). Range-dependent checks (live
   terminal data exchange, Guacamole) are out of scope.
9. **Workers**: the SQS worker and CTF scheduler boot from the same image and
   produce their `/tmp/<name>-heartbeat` files.

The order is migrations → released policy service/model → negative runtime gate
→ native operator cutover/bootstrap/verification → normal web entrypoint/readiness
→ real operator login → owning token issuance → native inbox registration →
distinct first browser login → websocket/pages → worker entrypoints.
The operator and first-login identities are distinct; no browser user or session
is seeded. Maintenance mode is limited to the operator setup container, and is
never passed to the web, inbox or worker containers.

### Local trust boundary

All identities, database passwords, provider keys and tokens are synthetic and
disposable. The trusted local host and Docker daemon control the fixture. The
OpenFGA images and CLI use the same released digests as the integration harness;
the chart-owned SQL enforces runtime/admin separation. Only the runtime token
and CA certificate are mounted into application containers. The token and
explicit identity facts are private mode-0600 files readable by the image user;
the admin token never enters application containers. The temporary TLS private
key is readable by the disposable OpenFGA image, matching the local integration
fixture; it is not a production certificate. Admin API ports bind only loopback.
The OIDC provider is the sole identity-provider double; its private-network HTTP
transport is a local fixture assumption, not a deployment configuration.

### Local OIDC provider double

Real login is exercised without any live identity provider (the blocking
Quality gate stays hosted and credential-free):

- `stub_idp.py`: a deterministic, fail-closed, Cognito-shaped OIDC provider
  double. It serves the exact endpoint shapes `config._oidc_settings` derives
  (`/oauth2/authorize`, `/oauth2/token`, `/oauth2/userInfo`,
  `/.well-known/jwks.json`), signs an RS256 ID token bound to the per-request
  `state`/`nonce`, and rejects the wrong client id/secret, a wrong/absent
  redirect URI, a reused code, or a missing bearer token. It runs under the
  built portal image's interpreter (reusing its in-image `PyJWT`/`cryptography`)
  and generates its keypair fresh at startup, with no committed private key.
  Standard `login_hint` selects only the explicit synthetic operator; absence
  selects the separate first-login identity. Each code and access token retains
  its exact subject/email, so UserInfo cannot switch identities.
- `oidc_login.py`: a browser-like login driver (cookie jar, manual redirect
  following) that walks `/login/`, authorize, and callback, then captures the
  callback-established session. It runs inside the smoke network so it reaches
  the portal and the IdP by name, and addresses the portal by its logical HTTPS
  origin (`Host` + `X-Forwarded-Proto: https`) over the private HTTP transport,
  so the secure-cookie / redirect-URI semantics stay real.

The captured session key is written to a mode-0600 file whose **path** (never
its value) is passed to `ws_handshake.py` / `page_smoke.py` via `--session-file`,
so no session/token material lands on any process's argv.

## Run it locally

```bash
# from the repo root; requires docker, uv, python3, curl, openssl
bash scripts/stack-smoke/stack_smoke.sh
```

The script refuses existing container/network names, creates a private docker
network, and tears down only resources labelled as owned by its run on exit
(including failures, after a bounded log tail), and exits non-zero on any failed
assertion.

## Parameters (extensibility seam)

All scalars are env-overridable; the worker/scheduler set is one list, so a new
variation is a parameter change rather than a copy of the workflow block.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMOKE_IMAGE` | `shifter-portal:stack-smoke` | Image tag to build/run |
| `SMOKE_BUILD` | `1` | Build the image (`0` = reuse `SMOKE_IMAGE`) |
| `SMOKE_DOCKERFILE` | `shifter/shifter_platform/Dockerfile` | Dockerfile path |
| `SMOKE_CONTEXT` | `shifter` | Docker build context |
| `SMOKE_WEB_PORT` | `18000` | Host port mapped to the web container's 8000 |
| `SMOKE_HEALTH_PATH` | `/health/` | Readiness path to poll for 200 |
| `SMOKE_WS_PATH` | `ws/notifications/` | Routed websocket consumer path |
| `SMOKE_PAGES` | SPA root/range/terminal/settings/help | Space-separated authenticated page paths to render + asset-check |
| `SMOKE_BOOT_TIMEOUT` | `180` | Seconds to wait for readiness |
| `SMOKE_HEARTBEAT_TIMEOUT` | `120` | Seconds to wait for each heartbeat |
| `SMOKE_WORKER_SPECS` | cms worker + ctf-scheduler | `name\|heartbeat_file\|command` per line |
| `SMOKE_PG_IMAGE` / `SMOKE_REDIS_IMAGE` / `SMOKE_ELASTICMQ_IMAGE` | pinned | Dependency double images |
| `SMOKE_IDP_PORT` | `8080` | Port the local OIDC provider double listens on |

The OIDC login scenario is a single parameter set (client id/secret, issuer,
auth domain, callback, synthetic claims) on one provider double, so the next
variation (another fixture version or synthetic claim set) is a parameter
change rather than a second harness.

## CI

`.github/workflows/_quality.yml` runs this as the `stack-smoke` job, gated on the
`run_stack_smoke` input that `.github/workflows/deploy.yml` drives from the
existing `portal_image` / `shifter_platform` path filters, so engine-only or
docs-only changes never pay the image build.
