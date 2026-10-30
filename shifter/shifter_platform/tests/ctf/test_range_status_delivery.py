"""A failed capacity return keeps the participant linkage until durable retry."""

import pytest
from django.db import OperationalError, connection
from django.utils import timezone

from cms.handlers.ctf_bridge import drain_ctf_status_deliveries, notify_ctf_range_status
from cms.models import RangeStatusDelivery
from cms.signals import range_status_changed
from ctf.models import CTFSpareRange

pytestmark = pytest.mark.django_db


def test_capacity_release_failure_retains_participant_linkage_for_retry(ctf_participant):
    ctf_participant.range_instance_id = 12345
    ctf_participant.range_status = "destroying"
    ctf_participant.save(update_fields=["range_instance_id", "range_status", "updated_at"])

    def unavailable_capacity(execute, sql, params, many, context):
        if "engine_capacity_draw" in sql:
            raise OperationalError("SYNTHETIC_DATABASE_DETAIL")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(unavailable_capacity):
        notify_ctf_range_status(12345, "destroyed", "destroying", cleanup_verified=True)

    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id == 12345
    assert ctf_participant.range_status == "destroying"
    delivery = RangeStatusDelivery.objects.get()
    assert delivery.last_error_type == "OperationalError"
    RangeStatusDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
    assert drain_ctf_status_deliveries() == 1
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id is None
    assert ctf_participant.range_status == ""


def test_spare_update_rolls_back_with_later_receiver_failure_and_replays(ctf_event):
    spare = CTFSpareRange.objects.create(event=ctf_event, range_instance_id=12345)

    def unavailable_receiver(sender, **kwargs):
        raise OperationalError("SYNTHETIC_DATABASE_DETAIL")

    range_status_changed.connect(unavailable_receiver, weak=False)
    try:
        notify_ctf_range_status(12345, "ready", "provisioning")
    finally:
        range_status_changed.disconnect(unavailable_receiver)

    spare.refresh_from_db()
    assert spare.status == "provisioning"
    assert RangeStatusDelivery.objects.get().last_error_type == "OperationalError"
    RangeStatusDelivery.objects.update(next_attempt_at=timezone.now())
    assert drain_ctf_status_deliveries() == 1
    spare.refresh_from_db()
    assert spare.status == "ready"
    assert not RangeStatusDelivery.objects.exists()
