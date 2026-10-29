"""Atomic warm count reservation and capacity draw, before provider work."""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import connection, transaction

if TYPE_CHECKING:
    from engine.models import WarmRangeGeneration
    from shared.warm_pool.policy import WarmPoolBucketPolicy, WarmPoolRuntimePolicy

    from ._warm_pool import WarmGenerationDraft

# Same transaction-scoped mutex pattern as CMS lease-policy publication. This
# namespace belongs to Engine and covers absent rows and cross-bucket ceilings.
_LOCK_NAMESPACE = 0x53484654
_WARM_ADMISSION_KEY = 232128


class _CapacityRefused(Exception):
    """Roll back an unavailable slot or partially admitted draw without publishing a generation."""


def reserve_warm_generation(draft: WarmGenerationDraft) -> WarmRangeGeneration | None:
    """Reserve one configured slot and its draw together, or make no changes."""
    from engine.models import WarmRangeGeneration

    from ._warm_pool import (
        admit_warm_generation_capacity,
        create_warm_generation,
    )

    try:
        with transaction.atomic():
            _lock_admission()
            policy, bucket = _admitted_bucket(draft)
            existing = WarmRangeGeneration.objects.filter(request_id=draft.request_id).first()
            if existing is not None:
                if any(getattr(existing, item.name) != getattr(draft, item.name) for item in fields(draft)):
                    raise ValueError("Warm reservation replay changed")
                return existing if existing.state in {"provisioning", "ready"} else None
            _require_available_slot(policy, bucket)
            generation = create_warm_generation(draft)
            if admit_warm_generation_capacity(
                scope_ref=draft.capacity_scope_ref, draw_key=draft.capacity_draw_key
            ).blocking:
                # One rejected metric must roll back other metrics' partial draws.
                raise _CapacityRefused
            return generation
    except _CapacityRefused:
        return None


def _lock_admission() -> None:
    """Serialize absent-row and cross-bucket count-and-reserve decisions in PostgreSQL."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [_LOCK_NAMESPACE, _WARM_ADMISSION_KEY])


def _admitted_bucket(draft: WarmGenerationDraft) -> tuple[WarmPoolRuntimePolicy, WarmPoolBucketPolicy]:
    """Bind the proposed generation to one current configured bucket before checking replay or limits."""
    from ._warm_pool import warm_capacity_scope_ref

    policy = settings.WARM_POOL_POLICY
    bucket = next((item for item in policy.buckets if item.id == draft.bucket_id), None)
    if (
        not policy.is_active()
        or bucket is None
        or bucket.backend != draft.backend
        or bucket.capacity_partition != draft.capacity_partition
        or draft.capacity_scope_ref != warm_capacity_scope_ref(settings.WARM_POOL_DEPLOYMENT_NAME, bucket.id)
    ):
        raise _CapacityRefused
    return policy, bucket


def _require_available_slot(policy: WarmPoolRuntimePolicy, bucket: WarmPoolBucketPolicy) -> None:
    """Check bucket and deployment ceilings while holding the single admission mutex."""
    from ._warm_pool import active_generation_count, total_active_generation_count

    ceiling = policy.max_total_ready or sum(item.maximum for item in policy.buckets)
    if (
        active_generation_count(bucket.id) >= min(bucket.target, bucket.maximum)
        or total_active_generation_count() >= ceiling
    ):
        raise _CapacityRefused
