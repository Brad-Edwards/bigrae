"""Warm-pool reconciler: converge the pool toward its declared policy (#28).

Deployment-owned, provider-neutral. Each pass, for every bucket in the effective
deployment policy, it:

- **finalizes** retiring generations whose range destroy has been observed,
  releasing their held capacity only then (never at claim or destroy-request time);
- **retires** ready generations whose warm-idle deadline passed or whose
  compatibility digest no longer matches the bucket's current realization identity
  (a scenario/image/config change), through the canonical destroy lifecycle; and
- **replenishes** a shortfall by warm-preparing new system-owned, quarantined
  generations up to the bucket target, bounded by the bucket maximum, the
  deployment total-ready ceiling, and the per-pass concurrency.

The ledger is an Engine-owned model, so every generation query/mutation goes
through the ``engine.services`` seam (ADR-001); this module owns only the CMS
reservation + RAES warm-prepare dispatch that the Engine cannot perform, plus the
policy-driven decisions. A warm generation is an installation-scoped RAES range
owned by the configured service principal; its ledger row is created at reservation
time so the provision suppresses participant access (quarantine). No provider I/O
happens in a transaction: provision and destroy travel through durable RAES intents.

The reconciler prepares for the account-scoped Mission Control live-fire target
class and builds its compatibility digest from the same
:class:`shared.warm_pool.compatibility.CompatibilityKey` dimensions the launch
claim path uses, so a generation it prepares is claimable by exactly those launches.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cms.services._warm_pool_claim import ISOLATION_ACCOUNT
from shared.enums import RangeSource, ResourceStatus
from shared.identity_scope import ResourceScope
from shared.operation_envelope import canonical_payload_digest
from shared.range_instantiation_policy import InstantiationPurpose, backend_supports_warm_activation
from shared.warm_pool.compatibility import CompatibilityKey, compatibility_digest
from shared.warm_pool.policy import WarmPoolBucketPolicy, WarmPoolRuntimePolicy

if TYPE_CHECKING:
    from shared.range_instantiation_policy import BackendAdmission

logger = logging.getLogger(__name__)

# The warm target class the reconciler prepares for; the launch claim path must
# derive the same values for a claim to match (documented in _warm_pool_claim).
WARM_RANGE_SOURCE = RangeSource.MISSION_CONTROL
WARM_PURPOSE = InstantiationPurpose.LIVE_FIRE
_WARM_EGRESS_MODE = "none"


def _policy_fingerprint(policy: WarmPoolRuntimePolicy, bucket: WarmPoolBucketPolicy) -> str:
    """Return a stable fingerprint of the effective policy a generation is minted under."""
    return canonical_payload_digest(
        {
            "scale_down": policy.scale_down,
            "replacement": policy.replacement,
            "bucket": {
                "id": bucket.id,
                "backend": bucket.backend,
                "scenario": bucket.scenario,
                "capacity_partition": bucket.capacity_partition,
                "target": bucket.target,
                "minimum": bucket.minimum,
                "maximum": bucket.maximum,
                "idle_ttl_seconds": bucket.idle_ttl_seconds,
                "region": bucket.region or "",
                "access_mode": bucket.access_mode or "",
            },
        }
    )


def _bucket_compatibility_digest(bucket: WarmPoolBucketPolicy, *, package_digest: str, lock_digest: str) -> str:
    """Compute the compatibility digest a generation in this bucket must carry."""
    return compatibility_digest(
        CompatibilityKey(
            backend=bucket.backend,
            instantiation_purpose=WARM_PURPOSE.value,
            range_source=WARM_RANGE_SOURCE.value,
            workspace_isolation_class=ISOLATION_ACCOUNT,
            egress_mode=_WARM_EGRESS_MODE,
            scenario=bucket.scenario,
            package_digest=package_digest,
            lock_digest=lock_digest,
        )
    )


def _warm_pool_credential():
    """Resolve the configured service credential's current lifecycle state."""
    from management.services import resolve_service_credential_reference

    reference = settings.WARM_POOL_SERVICE_CREDENTIAL_UUID
    if reference is None:
        raise ValueError("Warm-pool service credential unavailable")
    return resolve_service_credential_reference(reference, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE)


def reconcile_warm_pool(*, now: datetime | None = None) -> dict[str, int]:
    """Run one warm-pool reconcile pass. Returns a bounded per-outcome summary."""
    moment = now or timezone.now()
    policy: WarmPoolRuntimePolicy = settings.WARM_POOL_POLICY
    summary = {"provisioned": 0, "retired": 0, "finalized": 0, "buckets": 0}
    if not policy.is_active():
        return summary
    from engine.services import retire_removed_bucket_generations

    for bucket in policy.buckets:
        summary["buckets"] += 1
        try:
            _reconcile_bucket(bucket, policy, moment, summary)
        except Exception:
            # A single bucket's failure must not stop the others; the next pass
            # retries. Bounded, sanitized log only.
            logger.exception("warm-pool reconcile failed for bucket=%s", bucket.id)
    # Retire generations whose bucket was removed/narrowed out of the effective
    # policy, so they stop being claimable rather than lingering globally.
    summary["retired"] += retire_removed_bucket_generations([b.id for b in policy.buckets])
    _emit_pool_gauges(policy)
    return summary


def _emit_pool_gauges(policy: WarmPoolRuntimePolicy) -> None:
    """Publish per-bucket pool-depth gauges to the WarmPool metrics namespace (#28)."""
    from engine.services import bucket_state_counts
    from shared.warm_pool.metrics import WarmPoolBucketSnapshot, emit_gauges

    snapshots = []
    for bucket in policy.buckets:
        counts = bucket_state_counts(bucket.id)
        snapshots.append(
            WarmPoolBucketSnapshot(
                bucket_id=bucket.id,
                backend=bucket.backend,
                region=bucket.region or "",
                ready=counts.get("ready", 0),
                provisioning=counts.get("provisioning", 0),
                unhealthy=counts.get("unhealthy", 0),
                claimed=counts.get("claimed", 0),
            )
        )
    emit_gauges(snapshots)


def _reconcile_bucket(
    bucket: WarmPoolBucketPolicy, policy: WarmPoolRuntimePolicy, moment: datetime, summary: dict[str, int]
) -> None:
    """Converge one bucket: finalize, recover stalled, retire stale/excess, replenish."""
    from engine.services import (
        active_generation_count,
        finalize_retiring_generations,
        recover_stalled_generations,
    )

    summary["finalized"] += finalize_retiring_generations(bucket.id)
    # Crash-safety: retire generations stuck non-terminal (a failed prepare/activate
    # or a dispatch that crashed before any result) so no strand keeps counting
    # toward the ceiling. Grace scales with the replenish cadence.
    stall_grace = max(600, policy.replenish_interval_seconds * 3)
    summary["retired"] += recover_stalled_generations(bucket.id, stall_grace_seconds=stall_grace, now=moment)

    identity = _resolve_bucket_identity(bucket)
    if identity is None:
        # The bucket's scenario no longer resolves to a registered source: retire
        # every unclaimed generation and provision nothing.
        summary["retired"] += _retire_all_ready(bucket)
        return
    package_digest, lock_digest = identity
    current_digest = _bucket_compatibility_digest(bucket, package_digest=package_digest, lock_digest=lock_digest)

    live_ready, retired = _retire_stale_ready(bucket, current_digest, moment)
    summary["retired"] += retired
    active = active_generation_count(bucket.id)
    active, retired = _scale_down_excess(bucket, policy, live_ready, active)
    summary["retired"] += retired
    summary["provisioned"] += _replenish_shortfall(bucket, policy, current_digest, active)


def _retire_all_ready(bucket: WarmPoolBucketPolicy) -> int:
    """Retire every unclaimed generation in a bucket (its scenario no longer resolves)."""
    from engine.services import ready_generations, retire_generation

    retired = 0
    for gen in ready_generations(bucket.id):
        if retire_generation(gen):
            retired += 1
    return retired


def _retire_stale_ready(bucket: WarmPoolBucketPolicy, current_digest: str, moment: datetime) -> tuple[list[Any], int]:
    """Retire ready generations past their idle deadline or carrying a stale digest.

    Returns ``(survivors, retired_count)`` -- the still-live ready generations plus
    how many were retired.
    """
    from engine.services import ready_generations, retire_generation

    live_ready: list[Any] = []
    retired = 0
    for gen in ready_generations(bucket.id):
        expired = gen.idle_deadline is not None and gen.idle_deadline <= moment
        incompatible = gen.compatibility_digest != current_digest
        if expired or incompatible:
            if retire_generation(gen):
                retired += 1
        else:
            live_ready.append(gen)
    return live_ready, retired


def _scale_down_excess(
    bucket: WarmPoolBucketPolicy, policy: WarmPoolRuntimePolicy, live_ready: list[Any], active: int
) -> tuple[int, int]:
    """Retire unclaimed generations above target per the scale-down order.

    Returns ``(active, retired_count)`` with ``active`` decremented for each retirement.
    """
    from engine.services import retire_generation

    retired = 0
    if active > bucket.target:
        excess = active - bucket.target
        victims = live_ready if policy.scale_down == "oldest-first" else list(reversed(live_ready))
        for gen in victims[:excess]:
            if retire_generation(gen):
                retired += 1
                active -= 1
    return active, retired


def _replenish_shortfall(
    bucket: WarmPoolBucketPolicy, policy: WarmPoolRuntimePolicy, current_digest: str, active: int
) -> int:
    """Warm-prepare up to the shortfall, bounded by maximum, ceiling, and concurrency.

    Returns the number of generations dispatched this pass.
    """
    from engine.services import total_active_generation_count

    ceiling = policy.max_total_ready or sum(b.maximum for b in policy.buckets)
    ceiling_headroom = max(0, ceiling - total_active_generation_count())
    budget = min(
        max(0, bucket.target - active),
        max(0, bucket.maximum - active),
        ceiling_headroom,
        policy.replenish_concurrency,
    )
    provisioned = 0
    for _ in range(budget):
        if _provision_warm_generation(bucket, policy, current_digest):
            provisioned += 1
    return provisioned


def _resolve_bucket_identity(bucket: WarmPoolBucketPolicy) -> tuple[str, str] | None:
    """Return the (package_digest, lock_digest) for the bucket's scenario, or None."""
    from cms.models import RaesPackageSource

    source = RaesPackageSource.objects.filter(scenario_id=bucket.scenario).order_by("-pk").first()
    if source is None or not source.package_digest or not source.lock_digest:
        logger.warning("warm-pool: bucket=%s scenario=%s has no registered source", bucket.id, bucket.scenario)
        return None
    return source.package_digest, source.lock_digest


def _provision_warm_generation(bucket: WarmPoolBucketPolicy, policy: WarmPoolRuntimePolicy, compatibility: str) -> bool:
    """Warm-prepare one system-owned, quarantined generation for a bucket.

    Returns True on a dispatched preparation. Failures are logged and swallowed so
    one bad preparation does not abort the pass; capacity drawn for a failed
    dispatch is released.
    """
    from cms.services._range_backend_admission import assert_backend_admitted

    # Admission FIRST, before allocating any managed user, so an unsupported or
    # mismatched bucket (explicitly-accepted configuration) does not leak an
    # inactive managed user every reconcile pass.
    backend_admission = assert_backend_admitted(WARM_PURPOSE, WARM_RANGE_SOURCE)
    if (
        backend_admission is None
        or backend_admission.backend != bucket.backend
        or not backend_supports_warm_activation(backend_admission.backend)
    ):
        logger.warning("warm-pool: bucket=%s backend not warm-capable; skipping", bucket.id)
        return False
    return _warm_prepare_dispatch(bucket, policy, compatibility, backend_admission)


def _warm_prepare_dispatch(
    bucket: WarmPoolBucketPolicy,
    policy: WarmPoolRuntimePolicy,
    compatibility: str,
    backend_admission: BackendAdmission,
) -> bool:
    """Reserve, draw capacity, and dispatch one warm-prepare (helper of ``_provision_warm_generation``).

    Returns True on a dispatched preparation, False on a capacity refusal or any
    failure. All allocated capacity and reservation state is cleaned up on the
    non-dispatch paths.
    """
    from cms.services._raes_range_create import _load_raes_source_or_raise
    from engine.services import (
        WarmGenerationDraft,
        admit_warm_generation_capacity,
        create_warm_generation,
        warm_capacity_scope_ref,
    )

    draw_key = uuid4()
    scope_ref = warm_capacity_scope_ref(settings.WARM_POOL_DEPLOYMENT_NAME, bucket.id)
    request_id: UUID | None = None
    try:
        source = _load_raes_source_or_raise(bucket.scenario)
        request_id = uuid4()
        credential = _reserve_warm_intent(_warm_pool_credential(), source, request_id)

        # Create the ledger row BEFORE dispatch so the provision suppresses
        # participant access (quarantine), and draw capacity before provider work.
        create_warm_generation(
            WarmGenerationDraft(
                bucket_id=bucket.id,
                compatibility_digest=compatibility,
                effective_policy_fingerprint=_policy_fingerprint(policy, bucket),
                backend=bucket.backend,
                range_source=WARM_RANGE_SOURCE.value,
                capacity_partition=bucket.capacity_partition,
                capacity_scope_ref=scope_ref,
                capacity_draw_key=draw_key,
                request_id=request_id,
                idle_deadline=timezone.now() + timedelta(seconds=bucket.idle_ttl_seconds),
            )
        )
        # Capacity admission is a real gate, not advisory: a refused (REJECTED)
        # capacity or cost assessment must stop the warm provision. Clean up the
        # reserved generation and skip rather than dispatching over the ceiling.
        admission = admit_warm_generation_capacity(scope_ref=scope_ref, draw_key=draw_key)
        if admission.blocking:
            logger.warning("warm-pool: capacity refused for bucket=%s; skipping preparation", bucket.id)
            _abandon_preparation(request_id, draw_key)
            return False

        from cms.services._raes_dispatch import (
            RaesDispatchOptions,
            dispatch_object_raes_package,
            dispatch_repo_raes_package,
        )

        dispatch = dispatch_object_raes_package if source.source_kind == "object" else dispatch_repo_raes_package
        dispatch(
            request_id,
            credential,
            source,
            RaesDispatchOptions(
                backend_admission,
                None,
                _WARM_EGRESS_MODE,
                principal_scope=ResourceScope("installation"),
            ),
        )
        return True
    except Exception:
        logger.exception("warm-pool: failed to prepare a generation for bucket=%s", bucket.id)
        _abandon_preparation(request_id, draw_key)
        return False


def _reserve_warm_intent(credential, source, request_id: UUID):
    """Persist one installation-scoped service-owned intent before provider work."""
    from cms.models import RaesPackageSource, RangeInstance, Request
    from cms.services._pack_fence import fence_pack_reference
    from cms.services._scoped_range_launch import revalidate_scoped_launch, source_launch_binding
    from shared.credentials import resolve_local_credential
    from shared.operation_authority import credential_reference_fields

    scope = ResourceScope("installation")
    current = revalidate_scoped_launch(credential, scope, source)
    with transaction.atomic():
        if (
            resolve_local_credential(current) != current
            or not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists()
        ):
            raise ValueError("Warm-pool authority changed")
        fence_pack_reference(source.scenario_id, expected_digest=source.package_digest)
        fields = {
            "scope_kind": "installation",
            "account_id": None,
            "organization_id": None,
            "workspace_id": None,
            "owner_principal_uuid": current.principal.uuid,
        }
        request = Request.objects.create(
            request_id=request_id,
            request_type="range",
            user=None,
            **credential_reference_fields(current),
            **fields,
        )
        RangeInstance.objects.create(
            request=request,
            scenario_id=source.scenario_id,
            user_id=None,
            range_source=WARM_RANGE_SOURCE.value,
            status=ResourceStatus.PROVISIONING.value,
            range_spec=None,
            model_sources={"aliases": []},
            model_package_digest=source.package_digest,
            **fields,
        )
    return current


def _abandon_preparation(request_id: UUID | None, draw_key: UUID) -> None:
    """Clean up a warm preparation that was refused or failed before/at dispatch (#28).

    Releases the capacity draw, moves any created ledger row to a reconciler-owned
    retiring state and fails the CMS reservation so no stranded PROVISIONING row
    keeps counting toward the pool ceiling. Every step is best-effort and
    idempotent so cleanup never raises out of the pass.
    """
    from cms.models import RangeInstance
    from engine.services import release_warm_generation_capacity, retire_generations_for_request

    try:
        release_warm_generation_capacity(draw_key)
    except Exception:
        logger.exception("warm-pool cleanup: capacity release failed")
    if request_id is not None:
        try:
            retire_generations_for_request(request_id)
        except Exception:
            logger.exception("warm-pool cleanup: ledger retire failed")
        try:
            RangeInstance.objects.filter(request__request_id=request_id).update(status=ResourceStatus.FAILED.value)
        except Exception:
            logger.exception("warm-pool cleanup: range-instance fail-mark failed")
