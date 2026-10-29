"""Bounded recovery of retained warm obligations, independent of policy."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmRecoveryProgress:
    """Counts and a non-authoritative continuation for one retained-ledger page."""

    retired: int
    finalized: int
    next_cursor: int


def reconcile_retained_warm_generations(
    active_bucket_ids: tuple[str, ...],
    *,
    now: datetime,
    stall_grace_seconds: int,
    after_pk: int = 0,
    batch_size: int = 100,
) -> WarmRecoveryProgress:
    """Advance one keyset page; the managed worker retains the next page cursor."""
    from engine.models import Range, WarmRangeGeneration

    from ._warm_pool import _finalize_warm_generation, _generation_is_stalled, retire_generation

    limit = max(1, min(batch_size, 100))
    rows = list(
        WarmRangeGeneration.objects.filter(pk__gt=after_pk)
        .filter(
            Q(state__in=["provisioning", "ready", "claimed", "unhealthy", "retiring"])
            | Q(state="activated", range__status="destroyed")
            | Q(state="activated", range__isnull=True)
        )
        .order_by("pk")[:limit]
    )
    cutoff = now - timedelta(seconds=stall_grace_seconds)
    retired = finalized = 0
    for generation in rows:
        try:
            if generation.bucket_id not in active_bucket_ids and generation.state in {"provisioning", "ready"}:
                retired += retire_generation(generation)
            elif generation.state in {"provisioning", "claimed", "unhealthy"}:
                runtime = Range.objects.filter(request__request_id=generation.request_id).first()
                if _generation_is_stalled(generation, runtime, cutoff):
                    retired += retire_generation(generation, _recovery_cutoff=cutoff)
            finalized += _finalize_warm_generation(generation, now=now)
        except Exception:
            # One failed obligation must not prevent later retained work. No
            # authority is inferred from this cursor or from a skipped failure.
            logger.exception("warm-pool: retained recovery failed generation=%s", generation.uuid)
    return WarmRecoveryProgress(retired, finalized, rows[-1].pk if len(rows) == limit else 0)
