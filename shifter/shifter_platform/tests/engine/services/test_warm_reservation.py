"""Engine count admission commits the warm slot and capacity draw together."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.utils import timezone


def reservation_inputs(settings, *, global_limit=1, bucket_limit=1):
    from engine.models import CapacityReservation
    from engine.services import WarmGenerationDraft, warm_capacity_scope_ref
    from shared.warm_pool.policy import WarmPoolBucketPolicy, WarmPoolRuntimePolicy

    settings.WARM_POOL_DEPLOYMENT_NAME = "synthetic-reservation"
    settings.WARM_POOL_POLICY = WarmPoolRuntimePolicy(
        enabled=True,
        max_total_ready=global_limit,
        buckets=tuple(
            WarmPoolBucketPolicy(name, "gce", "synthetic", "synthetic", bucket_limit, 0, bucket_limit, 3600)
            for name in ("first", "second")
        ),
    )
    now = timezone.now()
    drafts = []
    for bucket in settings.WARM_POOL_POLICY.buckets:
        scope_ref = warm_capacity_scope_ref(settings.WARM_POOL_DEPLOYMENT_NAME, bucket.id)
        CapacityReservation.objects.create(
            event_ref=scope_ref,
            partition_name="synthetic",
            metric_name="vcpu",
            amount=10,
            unit_amount=1,
            consumed=0,
            enforcement="enforcing",
            window_start=now - timezone.timedelta(hours=1),
            window_end=now + timezone.timedelta(hours=1),
        )
        drafts.append(
            WarmGenerationDraft(
                bucket.id,
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                "gce",
                "mission_control",
                "synthetic",
                scope_ref,
                uuid4(),
                uuid4(),
                now + timezone.timedelta(hours=1),
            )
        )
    return tuple(drafts)


@pytest.mark.django_db
def test_refused_capacity_rolls_back_generation_and_partial_draw(settings):
    from engine.models import CapacityDraw, CapacityReservation, WarmRangeGeneration
    from engine.services import reserve_warm_generation

    draft, _ = reservation_inputs(settings)
    capacity = CapacityReservation.objects.get(event_ref=draft.capacity_scope_ref)
    capacity.pk = None
    capacity.metric_name = "z-unavailable"
    capacity.amount = 0
    capacity.save(force_insert=True)
    assert reserve_warm_generation(draft) is None
    assert not WarmRangeGeneration.objects.exists()
    assert not CapacityDraw.objects.exists()
    assert not CapacityReservation.objects.exclude(consumed=0).exists()


@pytest.mark.django_db
def test_admitted_slot_blocks_another_generation_before_provider_work(settings):
    from engine.models import CapacityDraw, WarmRangeGeneration
    from engine.services import reserve_warm_generation

    first, second = reservation_inputs(settings)
    original = reserve_warm_generation(first)
    assert original is not None
    assert reserve_warm_generation(first).pk == original.pk
    assert reserve_warm_generation(second) is None
    assert reserve_warm_generation(replace(first, request_id=uuid4(), capacity_draw_key=uuid4())) is None
    assert WarmRangeGeneration.objects.count() == CapacityDraw.objects.count() == 1
