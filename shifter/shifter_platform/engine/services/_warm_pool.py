"""Engine warm-pool allocation services (#28).

Two responsibilities live here, both database-only and provider-free so they are
safe on the launch path:

1. **Pool-scoped capacity** reuses the Engine capacity ledger
   (:mod:`engine.services._capacity_admit`) with a deterministic pool scope key
   instead of a fabricated CTF event (preflight #28). A warm generation draws
   from its bucket's scope budget when the operator has declared one, and
   otherwise no-ops exactly like any un-assessed range. Capacity is held while
   the generation's resources exist, including after claim, and released only
   after reconciliation observes provider absence.

2. **Atomic claim** transitions a ready, unclaimed, exact-fingerprint generation
   to ``CLAIMED`` under ``select_for_update(skip_locked=True)`` with a deterministic
   lock order. ``skip_locked`` gives one-winner semantics: two launches racing for
   the same single ready generation cannot both claim it — the loser sees no row
   and cold-falls-back. The claim is a plain transition; the CMS launch path wraps
   it together with ownership/workspace projection and the activation outbox in
   one transaction. Workers execute only after that transaction commits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.db import transaction
from django.db.models import Q

from engine.services._capacity_admit import admit_range_capacity, release_range_capacity
from shared.capacity import CapacityAssessmentResult

if TYPE_CHECKING:
    from engine.models import Range, WarmRangeGeneration

logger = logging.getLogger(__name__)

# Stable namespace so a bucket's capacity scope key is byte-identical across
# processes and restarts (mirrors the CTF spare draw namespace pattern).
_WARM_SCOPE_NAMESPACE = UUID("2a7b5c9e-3d81-4f26-8b14-6c0d9e2f1a55")

__all__ = [
    "WarmGenerationDraft",
    "active_generation_count",
    "admit_warm_generation_capacity",
    "bucket_state_counts",
    "claim_ready_generation",
    "create_warm_generation",
    "enqueue_range_activation",
    "finalize_retiring_generations",
    "ready_generations",
    "recover_stalled_generations",
    "release_warm_generation_capacity",
    "retire_generation",
    "retire_generations_for_request",
    "retire_removed_bucket_generations",
    "total_active_generation_count",
    "warm_capacity_scope_ref",
]


def enqueue_range_activation(request_id: UUID | str, *, credential=None) -> str:
    """Enqueue the ``raes-range activate`` operation for a claimed generation (#28).

    Called by the CMS claim path *after* the claim + ownership transfer commit, so
    a downstream conflict never leaves an activation queued against an unclaimed
    generation. Returns the durable launch-intent id. The provisioner realizes the
    claimant's fresh, sanitized access; the result applier transitions the range to
    READY for its new owner.
    """
    from engine.launch_intents import enqueue_provisioner_launch

    return enqueue_provisioner_launch(
        ["raes-range", "activate", "--request-id", str(request_id)],
        credential=credential,
    )


def warm_capacity_scope_ref(deployment_name: str, bucket_id: str) -> UUID:
    """Return the deterministic capacity scope key for one warm bucket.

    The scope is per (deployment, bucket) so each bucket's warm generations draw
    from a budget an operator can assess independently, without inventing a CTF
    event. Deterministic so a restart or a second reconciler derives the same key.
    """
    return uuid5(_WARM_SCOPE_NAMESPACE, f"{deployment_name}:{bucket_id}")


def admit_warm_generation_capacity(
    *, scope_ref: UUID, draw_key: UUID, now: datetime | None = None
) -> CapacityAssessmentResult:
    """Draw one warm generation's share from its bucket scope budget.

    Thin pass-through to :func:`admit_range_capacity`: the warm pool is a
    first-class capacity consumer, not a bypass. Returns the folded capacity
    outcome (``INDETERMINATE`` when the operator declared no budget for the scope,
    which does not block, exactly as for an un-assessed range).
    """
    return admit_range_capacity(scope_ref, draw_key=draw_key, now=now)


def release_warm_generation_capacity(draw_key: UUID, *, now: datetime | None = None) -> int:
    """Release a warm generation's capacity draw. Idempotent.

    Called only after reconciliation observes provider absence for the generation
    (ownership transfer does **not** release capacity — the resources still exist).
    """
    return release_range_capacity(draw_key, now=now)


def claim_ready_generation(
    *,
    candidates: list[tuple[str, str]],
    backend: str,
    range_source: str,
    claimant_request_id: UUID,
    now: datetime | None = None,
    expected_request_id: UUID | None = None,
) -> WarmRangeGeneration | None:
    """Atomically claim one ready generation from an authorized candidate set, or None.

    ``candidates`` is the launch's ``(bucket_id, compatibility_digest)`` pairs for the
    effective-policy buckets it is authorized to claim from -- so a launch can only
    receive a generation from a bucket the current server-side policy still permits,
    and only when the generation's realization/placement/posture digest matches
    (security: policy-to-allocation binding). The query is additionally bound to the
    invariant ``backend`` and ``range_source``.

    One-winner: ``select_for_update(skip_locked=True)`` plus a conditional
    ``READY -> CLAIMED`` transition means a generation is claimed by at most one launch
    (the partial-unique ``claimed_by_request_id`` constraint is the database backstop).
    A miss returns ``None`` and the caller cold-falls-back. Must run inside the caller's
    launch transaction so a downstream conflict rolls the claim back. An expired
    generation is never claimed; the reconciler retires it.
    """
    from django.utils import timezone

    from engine.models import WarmRangeGeneration

    moment = now or timezone.now()
    with transaction.atomic():
        candidate = (
            _ready_candidates(candidates, backend, range_source, moment)
            .select_for_update(skip_locked=True)
            .filter(Q(request_id=expected_request_id) if expected_request_id is not None else Q())
            .first()
        )
        if candidate is None:
            return None
        candidate.state = WarmRangeGeneration.State.CLAIMED
        candidate.claimed_by_request_id = claimant_request_id
        candidate.claimed_at = moment
        candidate.save(update_fields=["state", "claimed_by_request_id", "claimed_at"])
        return candidate


def _ready_candidates(candidates, backend, range_source, moment):
    from engine.models import WarmRangeGeneration

    authorized = Q(pk__in=[])
    for bucket_id, digest in candidates:
        authorized |= Q(bucket_id=bucket_id, compatibility_digest=digest)
    return (
        WarmRangeGeneration.objects.filter(
            authorized,
            state=WarmRangeGeneration.State.READY,
            backend=backend,
            range_source=range_source,
            claimed_by_request_id__isnull=True,
        )
        .filter(Q(idle_deadline__isnull=True) | Q(idle_deadline__gt=moment))
        .order_by("created_at", "pk")
    )


def preview_ready_generation(*, candidates, backend, range_source):
    """Read one eligible generation for pre-lock admission; claiming still rechecks every predicate."""
    from django.utils import timezone

    return _ready_candidates(candidates, backend, range_source, timezone.now()).first()


def ready_generations(bucket_id: str) -> list[WarmRangeGeneration]:
    """Return the READY (unclaimed) generations for a bucket, oldest first."""
    from engine.models import WarmRangeGeneration

    return list(
        WarmRangeGeneration.objects.filter(bucket_id=bucket_id, state=WarmRangeGeneration.State.READY).order_by(
            "created_at", "pk"
        )
    )


def active_generation_count(bucket_id: str) -> int:
    """Return the nonterminal-unclaimed (provisioning + ready) count for a bucket."""
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.filter(
        bucket_id=bucket_id, state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES
    ).count()


def total_active_generation_count() -> int:
    """Return the nonterminal-unclaimed count across every bucket (ceiling accounting)."""
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.filter(state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES).count()


@dataclass(frozen=True)
class WarmGenerationDraft:
    """The fields for a new PROVISIONING warm-generation ledger row (#28).

    Groups the realization identity and capacity-draw references so the reconciler
    hands the Engine one immutable value object instead of a wide argument list.
    """

    bucket_id: str
    compatibility_digest: str
    effective_policy_fingerprint: str
    backend: str
    range_source: str
    capacity_partition: str
    capacity_scope_ref: UUID
    capacity_draw_key: UUID
    request_id: UUID
    idle_deadline: datetime


def create_warm_generation(draft: WarmGenerationDraft) -> WarmRangeGeneration:
    """Create a PROVISIONING warm-generation ledger row keyed by request_id.

    Created at reservation time, before dispatch, so the warm-prepare provision
    suppresses participant access (quarantine).
    """
    from engine.models import WarmRangeGeneration

    return WarmRangeGeneration.objects.create(
        bucket_id=draft.bucket_id,
        compatibility_digest=draft.compatibility_digest,
        effective_policy_fingerprint=draft.effective_policy_fingerprint,
        backend=draft.backend,
        range_source=draft.range_source,
        capacity_partition=draft.capacity_partition,
        capacity_scope_ref=draft.capacity_scope_ref,
        capacity_draw_key=draft.capacity_draw_key,
        request_id=draft.request_id,
        state=WarmRangeGeneration.State.PROVISIONING,
        idle_deadline=draft.idle_deadline,
    )


def retire_generation(generation: WarmRangeGeneration, *, _recovery_cutoff: datetime | None = None) -> bool:
    """Retire one generation through the canonical destroy lifecycle.

    Marks RETIRING and requests destroy; capacity is released only when a later
    pass observes provider absence (:func:`finalize_retiring_generations`). Pool
    retirement is limited to unclaimed custody. Recovery may supply its stall
    cutoff for a failed or stalled claim, whose current runtime is checked again
    under the ledger lock. Returns True when a retirement was initiated.
    """
    from engine.models import WarmRangeGeneration

    with transaction.atomic():
        current = WarmRangeGeneration.objects.select_for_update().get(pk=generation.pk)
        fields = ("state", "request_id", "range_id", "claimed_by_request_id", "operation_id", "capacity_draw_key")
        if current.state in {"retiring", "terminal"} or any(
            getattr(current, name) != getattr(generation, name) for name in fields
        ):
            return False
        if current.claimed_by_request_id is not None:
            from engine.models import Range

            runtime = Range.objects.select_for_update().filter(request__request_id=current.request_id).first()
            if _recovery_cutoff is None or not _generation_is_stalled(current, runtime, _recovery_cutoff):
                return False
        current.state = WarmRangeGeneration.State.RETIRING
        current.save(update_fields=["state"])
        generation.state = current.state
    try:
        _request_warm_retirement(current)
    except Exception:
        # A dispatch failure is not fatal: the row stays RETIRING and
        # finalize_retiring_generations retries destroy on a later pass until
        # provider absence is observed.
        logger.exception("warm-pool: destroy dispatch failed for generation=%s (will retry)", generation.uuid)
    return True


def _request_warm_retirement(generation) -> None:
    from engine.models import Range, WarmRangeGeneration

    from ._principal_lifecycle import commit_prepared_range_lifecycle, prepare_system_range_cleanup
    from ._range_by_request import destroy_range_by_request

    with transaction.atomic():
        current = WarmRangeGeneration.objects.select_for_update().get(pk=generation.pk, state="retiring")
        if (current.request_id, current.capacity_draw_key) != (generation.request_id, generation.capacity_draw_key):
            raise ValueError("Warm retirement generation changed")
        runtime = Range.objects.filter(request__request_id=current.request_id).first()
        if runtime is None:
            return
        if current.range_id is not None and current.range_id != runtime.pk:
            raise ValueError("Warm retirement runtime changed")
        if runtime.owner_principal_uuid is not None:
            commit_prepared_range_lifecycle(prepare_system_range_cleanup(current.request_id))
            return
    destroy_range_by_request(generation.request_id)


def abandon_warm_preparation(request_id: UUID | None, draw_key: UUID) -> bool:
    """Release only never-realized work; late failures cannot retire customer custody."""
    from engine.models import Range, WarmRangeGeneration

    if request_id is None:
        release_range_capacity(draw_key)
        return True
    with transaction.atomic():
        generation = WarmRangeGeneration.objects.select_for_update().filter(request_id=request_id).first()
        if generation is not None:
            if generation.capacity_draw_key != draw_key or generation.state not in {
                "provisioning",
                "ready",
                "retiring",
            }:
                return False
            if generation.state != "retiring":
                retire_generation(generation)
        if Range.objects.filter(request__request_id=request_id).exists():
            return True
        release_range_capacity(draw_key)
        if generation is not None:
            generation.state = "terminal"
            from django.utils import timezone

            generation.retired_at = timezone.now()
            generation.save(update_fields=["state", "retired_at"])
        return True


def retire_removed_bucket_generations(active_bucket_ids: list[str]) -> int:
    """Retire unclaimed generations whose bucket is no longer in the effective policy (#28).

    When a bucket is removed from deployment policy, its already-ready generations
    must stop being claimable rather than lingering globally; this moves them to the
    canonical destroy lifecycle. Returns how many were retired.
    """
    from engine.models import WarmRangeGeneration

    retired = 0
    rows = WarmRangeGeneration.objects.filter(state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES).exclude(
        bucket_id__in=active_bucket_ids
    )
    for generation in rows:
        if retire_generation(generation):
            retired += 1
    return retired


def recover_stalled_generations(bucket_id: str, *, stall_grace_seconds: int, now: datetime | None = None) -> int:
    """Retire warm generations stuck in a non-terminal state, for crash-safety (#28).

    Reconciler-owned recovery for the fallible boundaries that a result-driven
    transition cannot close on its own:

    - an UNHEALTHY generation (a failed prepare/activation the applier flagged) is
      always dispatched to canonical destroy;
    - a PROVISIONING or CLAIMED generation whose realized Range is FAILED is retired;
    - a PROVISIONING generation with no realized Range older than the stall grace (a
      dispatch that crashed before any result) is retired; and
    - a CLAIMED generation whose Range is still merely PROVISIONING past the stall
      grace (a claim that committed but whose activation never completed -- e.g. an
      enqueue that failed after commit) is retired so the stuck claimant-owned
      generation is torn down rather than lingering.

    Returns how many were retired. Idempotent and bounded.
    """
    from datetime import timedelta

    from django.utils import timezone

    from engine.models import Range, WarmRangeGeneration

    moment = now or timezone.now()
    cutoff = moment - timedelta(seconds=stall_grace_seconds)
    retired = 0
    rows = WarmRangeGeneration.objects.filter(
        bucket_id=bucket_id,
        state__in=(
            WarmRangeGeneration.State.PROVISIONING,
            WarmRangeGeneration.State.CLAIMED,
            WarmRangeGeneration.State.UNHEALTHY,
        ),
    )
    for generation in rows:
        realized = (
            Range.objects.filter(request__request_id=generation.request_id).order_by("-created_at", "-pk").first()
        )
        if _generation_is_stalled(generation, realized, cutoff) and retire_generation(
            generation, _recovery_cutoff=cutoff
        ):
            retired += 1
    return retired


def _generation_is_stalled(generation: WarmRangeGeneration, realized: Range | None, cutoff: datetime) -> bool:
    """Return True when a non-terminal generation should be recovered (retired).

    A generation is stalled when it is UNHEALTHY, its realized range is FAILED, it
    never realized a range past the stall grace, or a CLAIMED generation's range is
    still merely PROVISIONING past the grace (activation never completed).
    """
    from engine.models import Range, WarmRangeGeneration

    unhealthy = generation.state == WarmRangeGeneration.State.UNHEALTHY
    failed = realized is not None and realized.status == Range.Status.FAILED
    never_realized_stalled = (
        generation.state == WarmRangeGeneration.State.PROVISIONING
        and realized is None
        and generation.created_at <= cutoff
    )
    claimed_activation_stalled = (
        generation.state == WarmRangeGeneration.State.CLAIMED
        and realized is not None
        and realized.status == Range.Status.PROVISIONING
        and generation.claimed_at is not None
        and generation.claimed_at <= cutoff
    )
    return unhealthy or failed or never_realized_stalled or claimed_activation_stalled


def retire_generations_for_request(request_id: UUID) -> int:
    """Retire every unclaimed warm generation for a request (failed-preparation cleanup).

    Moves PROVISIONING/READY rows for the request to RETIRING through the canonical
    destroy lifecycle. Returns how many were retired. Idempotent.
    """
    from engine.models import WarmRangeGeneration

    retired = 0
    rows = WarmRangeGeneration.objects.filter(
        request_id=request_id, state__in=WarmRangeGeneration.NONTERMINAL_UNCLAIMED_STATES
    )
    for generation in rows:
        if retire_generation(generation):
            retired += 1
    return retired


def finalize_retiring_generations(bucket_id: str, *, now: datetime | None = None) -> int:
    """Release capacity + mark TERMINAL once a generation's range is gone (#28).

    Handles both terminal paths so a warm capacity draw is never leaked:

    - a RETIRING generation whose range is destroyed (or was never realized) is a
      pool teardown; and
    - an ACTIVATED generation (a consumed warm launch) whose owned range the
      claimant later destroyed -- the warm draw *is* that range's capacity draw, so
      it is released only now, after provider absence, never at claim or
      destroy-request time (preflight #28).

    Returns the number finalized.
    """
    from django.utils import timezone

    from engine.models import WarmRangeGeneration

    moment = now or timezone.now()
    rows = WarmRangeGeneration.objects.filter(
        bucket_id=bucket_id,
        state__in=(WarmRangeGeneration.State.RETIRING, WarmRangeGeneration.State.ACTIVATED),
    )
    finalized = 0
    for generation in rows.iterator(chunk_size=100):
        try:
            finalized += _finalize_warm_generation(generation, now=moment)
        except Exception:
            logger.exception("warm-pool: finalization failed generation=%s", generation.uuid)
    return finalized


def _finalize_warm_generation(generation: WarmRangeGeneration, *, now: datetime) -> bool:
    from engine.models import Range, WarmRangeGeneration

    with transaction.atomic():
        generation = WarmRangeGeneration.objects.select_for_update().get(pk=generation.pk)
        if generation.state not in {"retiring", "activated"}:
            return False
        realized = Range.objects.filter(request__request_id=generation.request_id).order_by("-pk").first()
        gone = realized is None or realized.status == Range.Status.DESTROYED
        if gone and realized is not None and realized.owner_principal_uuid is not None:
            from ._cleanup_verification import is_range_generation_cleanup_verified_absent

            gone = realized.cms_range_uuid is not None and is_range_generation_cleanup_verified_absent(
                generation.request_id, realized.cms_range_uuid, realized.pk
            )
        if gone:
            release_range_capacity(generation.capacity_draw_key)
            generation.state = WarmRangeGeneration.State.TERMINAL
            generation.retired_at = now
            generation.save(update_fields=["state", "retired_at"])
            return True
        if realized is not None and realized.status == "destroyed" and realized.owner_principal_uuid is not None:
            from ._range_cleanup_retry import retry_range_cleanup_verification

            retry_range_cleanup_verification(generation.request_id, now=now)
        elif generation.state == "retiring" and realized is not None:
            _request_warm_retirement(generation)
    return False


def bucket_state_counts(bucket_id: str) -> dict[str, int]:
    """Return per-state generation counts for a bucket (metrics gauges, #28).

    Keys are the private allocation-state values; a state with no rows is 0. Used
    by the reconciler to publish pool-depth gauges without exposing the model.
    """
    from django.db.models import Count

    from engine.models import WarmRangeGeneration

    counts = {state.value: 0 for state in WarmRangeGeneration.State}
    rows = WarmRangeGeneration.objects.filter(bucket_id=bucket_id).values_list("state").annotate(n=Count("state"))
    for state, n in rows:
        counts[str(state)] = n
    return counts


def enqueue_admitted_warm_activation(prepared, *, request_id: UUID, claimant_request_id: UUID) -> str:
    """Commit activation with the exact already-admitted warm custody transition."""
    from engine.launch_intents import _enqueue_admitted_provisioner_launch
    from engine.models import Range, RangeAuthorityChange, WarmRangeGeneration
    from shared.credentials import resolve_local_credential

    from ._launch_authority import range_runtime_binding_digest
    from ._principal_warm_claim import PreparedWarmRangeClaim

    if (
        not isinstance(prepared, PreparedWarmRangeClaim)
        or resolve_local_credential(prepared.credential) != prepared.credential
    ):
        raise ValueError("Warm activation authority changed")
    generation = WarmRangeGeneration.objects.select_for_update().get(
        request_id=request_id, claimed_by_request_id=claimant_request_id, state="claimed"
    )
    if generation.range_id is None:
        raise ValueError("Warm generation runtime unavailable")
    row = Range.objects.select_for_update().get(pk=generation.range_id)
    if row.resource_generation is None:
        raise ValueError("Warm generation identity unavailable")
    retained = (
        RangeAuthorityChange.objects.filter(
            range=row,
            resource_generation=row.resource_generation,
            authority_binding_digest=range_runtime_binding_digest(row),
        )
        .order_by("-accepted_at", "-id")
        .first()
    )
    if (
        retained is None
        or retained.credential_uuid != prepared.credential.credential_uuid
        or retained.authority_binding_digest != range_runtime_binding_digest(row)
        or row.owner_principal_uuid != prepared.credential.principal.uuid
    ):
        raise ValueError("Warm activation binding changed")
    from shared.operation_authority import credential_reference_fields

    names = [field.name for field in RangeAuthorityChange._meta.fields if field.name.startswith("authority_")]
    fields = credential_reference_fields(prepared.credential) | {name: getattr(retained, name) for name in names}
    payload = {"version": 1, "resource": "raes-range", "operation": "activate", "request_id": str(request_id)}
    return _enqueue_admitted_provisioner_launch(
        ["raes-range", "activate", "--request-id", str(request_id)], payload, fields
    )
