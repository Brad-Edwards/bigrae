# Shifter Platform

Django application architecture for the Shifter cyber range platform.

- [Unified access credentials](access-credentials.md)—admission, lifecycle and renewable client setup.
- [Content, range and continuing access authority](content-range-authority.md)—scoped service operations, durable authority and native session termination.

## Ownership and lifecycle

The [current-state handbook](../architecture.md) owns the package/dependency,
trust, deployment and command/result/event views. The canonical layer
classification and per-symbol service allowances live in
[layer_imports.yaml](https://github.com/Brad-Edwards/shifter/blob/dev/scripts/check_layer_imports/layer_imports.yaml).

CMS owns content and desired lifecycle; Engine owns realized state and operation
application; CTF owns event/participant behavior; Management owns identity;
Workspaces owns customer ancestry; shared owns contracts and durable audit.
Mission Control is presentation and config is the composition root. Public
service facades carry validated scalar identities across domain boundaries.

Provisioner Jobs read immutable inputs and append results. Engine validates and
applies declared operation families, commits audit and outbox events, and workers
update projections. Redis/WebSocket notifications do not own business truth.
See the handbook for compatibility-family limits and staged identity activation.

- [CMS](cms.md)
- [Engine](engine.md)
- [Mission Control](mission_control.md)
- [Management](management.md)
- [Workspaces](workspaces.md)
- [Authorization](authorization.md)
- [CTF](ctf.md)

## Scenario Verification

The shared scenario-verification framework discovers an explicitly selected
installed plugin and runs its checks through a bounded, transport-neutral
runner. Core ships no scenario adapters or answer material.

- [Scenario-verification plugin guide](scenario-verification.md): public v1
  types, plugin factories, operator selection, execution, status/exit behavior,
  and report redaction.

## Organization/workspace admin console

The `/administer` SPA hosts a staff-only console shell over the workspace
tenancy layer, backed by a read-only current-principal context endpoint. It adds
routing, a workspace context/switcher, and capability-aware navigation without a
new authority model or feature flag.

- [Organization/workspace admin console](org-workspace-admin-console.md): the
  `/api/v1/workspaces/context/` projection, staff-session boundary, and SPA
  shell/routing/selection.
- [User lifecycle administration](user-lifecycle-administration.md): the
  `management.lifecycle` transition service and `UserProfile.suspended_at`
  discriminator, inactive-account authentication enforcement, the Django
  password-reset dispatcher, and the bounded ownership-transfer command
  (ADR-046-R13).
- [Range-to-workspace scoping administration](range-workspace-scoping-administration.md):
  the CMS list and expected-source compare-and-set rebind seams, the owner/admin
  workspace scope operations, the Engine compare-and-set facade, and the
  fail-closed handling of domain-owned aggregates (ADR-046-R14).

## Retry-safe range operations

- [Retry-safe range operations](retry-safe-range-operations.md): the caller
  retry identity, immutable-intent binding and canonical digest, the PostgreSQL-
  arbitrated retry binding, reauthorized replay/status/cancel, and truthful
  teardown/residual retention (ADR-063, #2086).

## Artifact preparation

The [artifact preparation design](https://github.com/Brad-Edwards/shifter/blob/dev/docs/architecture/raes-in-tenant-artifact-preparation-design-1583.md)
defines adapter ownership, private registration, worker isolation, independent
verification and inventory admission before ordinary range launch.

## Audit logging

The shared platform layer owns the durable audit store and writer. Audit rows
are visible through Django admin and the staff-session-only `/api/v1/audit/`
read API.

- [Audit system architecture](https://github.com/Brad-Edwards/shifter/blob/dev/docs/architecture/audit-system-architecture.md)
- [Administrator audit and activity history](admin-audit-activity.md): the
  hardened `/api/v1/audit/` read API, its typed filters, and the staff-facing
  `/administer/audit` SPA surface.

## Scoped communications

- [Communication API and receipt boundaries](ctf-communications.md)

- [Prepared account administration](account-administration.md) - Services, API, browser, migrations and evidence.
