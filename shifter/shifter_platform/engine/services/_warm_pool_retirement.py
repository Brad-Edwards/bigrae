"""Retained warm retirement, exact cleanup verification and capacity settlement."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import transaction

from ._capacity_admit import release_range_capacity

if TYPE_CHECKING:
    from engine.models import Range, WarmRangeGeneration

logger = logging.getLogger(__name__)


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


def _request_warm_retirement(generation: WarmRangeGeneration) -> None:
    """Request destroy for the exact retiring warm ledger and native runtime binding."""
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
        if not Range.objects.filter(request__request_id=request_id).exists():
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
    return unhealthy or failed or never_realized_stalled or _claim_activation_stalled(generation, realized, cutoff)


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
    """Settle one locked ledger only after exact cleanup, otherwise retain its retry obligation."""
    from engine.models import Range, WarmRangeGeneration

    with transaction.atomic():
        generation = WarmRangeGeneration.objects.select_for_update().get(pk=generation.pk)
        if generation.state not in {"retiring", "activated"}:
            return False
        realized = Range.objects.filter(request__request_id=generation.request_id).order_by("-pk").first()
        gone = _warm_cleanup_absent(generation, realized)
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


def _warm_cleanup_absent(generation: WarmRangeGeneration, realized: Range | None) -> bool:
    """A native destroyed flag settles capacity only with exact accepted generation evidence."""
    from engine.models import Range

    gone = realized is None or realized.status == Range.Status.DESTROYED
    if gone and realized is not None and realized.owner_principal_uuid is not None:
        from ._cleanup_verification import is_range_generation_cleanup_verified_absent

        gone = realized.cms_range_uuid is not None and is_range_generation_cleanup_verified_absent(
            generation.request_id, realized.cms_range_uuid, realized.pk
        )
    return gone


def _claim_activation_stalled(generation: WarmRangeGeneration, realized: Range | None, cutoff: datetime) -> bool:
    """Activation recovery ages the current customer claim, never the earlier pool preparation."""
    from engine.models import Range, WarmRangeGeneration

    return (
        generation.state == WarmRangeGeneration.State.CLAIMED
        and realized is not None
        and realized.status == Range.Status.PROVISIONING
        and generation.claimed_at is not None
        and generation.claimed_at <= cutoff
    )
