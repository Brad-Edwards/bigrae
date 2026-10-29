"""Durable local signal delivery: rollback, ordered retry and source retention."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from cms.handlers.ctf_bridge import drain_ctf_status_deliveries, notify_ctf_range_status
from cms.models import RangeStatusDelivery
from cms.signals import range_status_changed

pytestmark = pytest.mark.django_db


def test_failed_delivery_does_not_overtake_earlier_status_or_lose_cleanup_proof(caplog):
    observed = []
    unavailable = True

    def receiver(sender, new_status, cleanup_verified, **kwargs):
        observed.append((new_status, cleanup_verified))
        if unavailable:
            raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_TEXT")

    range_status_changed.connect(receiver, weak=False)
    try:
        notify_ctf_range_status(12345, "ready", "provisioning")
        notify_ctf_range_status(12345, "destroyed", "destroying", cleanup_verified=True)
        assert observed == [("ready", False)]
        assert RangeStatusDelivery.objects.count() == 2
        assert "SYNTHETIC_PRIVATE_PROVIDER_TEXT" not in caplog.text
        unavailable = False
        RangeStatusDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        assert drain_ctf_status_deliveries(limit=1) == 1
        assert drain_ctf_status_deliveries() == 1
        assert observed == [("ready", False), ("ready", False), ("destroyed", True)]
        assert not RangeStatusDelivery.objects.exists()
        assert drain_ctf_status_deliveries() == 0
    finally:
        range_status_changed.disconnect(receiver)


def test_delivery_is_not_created_when_the_owning_transaction_rolls_back():
    def receiver(sender, **kwargs):
        raise RuntimeError("unavailable")

    range_status_changed.connect(receiver, weak=False)
    try:
        with pytest.raises(ValueError, match="abort"), transaction.atomic():
            notify_ctf_range_status(12345, "ready", "provisioning")
            assert RangeStatusDelivery.objects.count() == 1
            raise ValueError("abort")
    finally:
        range_status_changed.disconnect(receiver)
    assert not RangeStatusDelivery.objects.exists()


def test_failed_range_does_not_block_delivery_to_another_range():
    def receiver(sender, range_instance_id, **kwargs):
        if range_instance_id == 12345:
            raise RuntimeError("unavailable")

    range_status_changed.connect(receiver, weak=False)
    try:
        notify_ctf_range_status(12345, "ready", "provisioning")
        notify_ctf_range_status(12346, "ready", "provisioning")
    finally:
        range_status_changed.disconnect(receiver)
    assert list(RangeStatusDelivery.objects.values_list("range_instance_id", flat=True)) == [12345]


def test_backed_off_range_does_not_fill_the_retry_batch():
    for index in range(3):
        RangeStatusDelivery.objects.create(
            range_instance_id=12345,
            new_status="ready",
            previous_status="provisioning",
            next_attempt_at=timezone.now() + timedelta(minutes=5) if index == 0 else timezone.now(),
        )
    RangeStatusDelivery.objects.create(range_instance_id=12346, new_status="ready", previous_status="provisioning")

    assert drain_ctf_status_deliveries(limit=1) == 1
    assert set(RangeStatusDelivery.objects.values_list("range_instance_id", flat=True)) == {12345}
