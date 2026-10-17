# Scenario contracts: CyberScript, ACES and RAES

This page records the transition requested by #1531 and the current supported
contract. Return to the [current-state handbook](../architecture.md).

## Current state

CyberScript is retired. Current authoring, package registration, catalog, API,
persistence and new provisioning use RAES. The platform currently pins
`raes==3.5.0` and `raes-env-packs==5.2.0` in its
[package manifest](https://github.com/Brad-Edwards/shifter/blob/dev/shifter/shifter_platform/pyproject.toml)
and lockfile. Check those files when upgrading; a historical page is not a
second pin registry.

Only `shared.raes` imports the released RAES module family. It validates and
compiles authored intent into the RAES ProvisioningPlan. The independent
provisioner validates and reads plain serialized data, not a second authored
schema. New realization accepts `raes-provisioning-plan-v2` with producer `3.5.0`.
Images, cloud resources, secrets and backend realization remain Shifter-owned.

## Transition history

| Stage | Status and meaning |
| --- | --- |
| CyberScript | Retired local authoring/provisioning implementation. Do not install it or import its former packages. |
| ACES integration | Historical contract name and earlier migration plans. Old parallel-rollout flags, aliases and parity inventories describe that period. |
| RAES hard cut | Current accepted ADR-024/031/032 contract. One package/catalog/API/provisioning vocabulary; no CyberScript fallback, dual reader or reversible selector. |

Existing content must be republished as a current environment pack and registered
through the normal CMS validator. Renaming old payload keys or labeling old
bytes as a new producer is not migration. Retired source/sidecar records are
removed by cutover migrations; terminal opaque historical envelopes remain
factual and inert.

Cleanup alone has a narrowly versioned reader for the previous exact
`raes-provisioning-plan-v1` / `2.0.0` pair. This preserves cleanup identities
without allowing old producers for new realization. It does not revive the
retired CyberScript or ACES path.

## Target and remaining qualification

The target stays one released RAES contract, with backend capabilities advertised
only when their admitted shapes have real guest effects and readback evidence.
The current
[backend manifest](https://github.com/Brad-Edwards/shifter/blob/dev/shifter/shifter_platform/shared/raes/backend-manifest.json)
and realization envelope bound claims. Parsing a field, retaining a projection,
or passing unit conformance is not proof of live scenario support. Private packs
and executable adapters remain separately owned and authorized.

Package upgrades require coordinated producer/consumer pins, serialized-plan
parity, genuine guest-effect verification and cleanup compatibility. Do not
advertise full catalog parity from a language/package rename.

## Rollback

Cutover waits until retired provisioning work is terminal and fails closed when
it cannot establish that condition. Rollback is an operator-controlled restore
of the preceding release and matching pre-cutover database backup, with workers
stopped. There is no in-process mixed-schema rollback flag.

The [hard-cut decision](https://github.com/Brad-Edwards/shifter/blob/dev/docs/architecture/raes-migration-adr.md)
and [ADR registry](https://github.com/Brad-Edwards/shifter/blob/dev/docs/adr/index.yaml)
retain the detailed migration and realization constraints. Earlier preflights
are historical records and do not override them.
