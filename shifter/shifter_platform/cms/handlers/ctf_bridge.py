"""Durably deliver CMS range status changes to local CTF receivers."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from cms.models import RangeInstance, RangeStatusDelivery
from cms.signals import range_status_changed

logger = logging.getLogger(__name__)


def notify_ctf_range_status(
    range_instance_id: int,
    new_status: str,
    previous_status: str,
    cleanup_verified: bool = False,
) -> None:
    """Persist delivery with the caller's CMS status write, then attempt it.

    A receiver failure rolls back its effects and retains the delivery for the
    existing range reconciler. Captured cleanup proof must survive replay: a
    logical DESTROYED status alone never authorizes capacity/linkage release.
    """
    RangeStatusDelivery.objects.create(
        range_instance_id=range_instance_id,
        new_status=new_status,
        previous_status=previous_status,
        cleanup_verified=cleanup_verified,
    )
    drain_ctf_status_deliveries(range_instance_id=range_instance_id)


def drain_ctf_status_deliveries(*, limit: int = 100, range_instance_id: int | None = None) -> int:
    """Retry bounded pending deliveries, preserving status order for each range."""
    earlier = RangeStatusDelivery.objects.filter(range_instance_id=OuterRef("range_instance_id"), pk__lt=OuterRef("pk"))
    pending = RangeStatusDelivery.objects.filter(~Exists(earlier), next_attempt_at__lte=timezone.now()).order_by("pk")
    if range_instance_id is not None:
        pending = pending.filter(range_instance_id=range_instance_id)
    identities = dict.fromkeys(pending.values_list("range_instance_id", flat=True)[:limit])
    delivered = 0
    for identity in identities:
        while delivered < limit and _deliver_next(identity):
            delivered += 1
    return delivered


def _deliver_next(range_instance_id: int) -> bool:
    """Commit receiver writes and delivery removal together, or retain a retry."""
    with transaction.atomic():
        # Use the same lock order as apply_range_status: CMS projection, delivery,
        # then receiver rows. The outbox survives a physically pruned projection.
        RangeInstance.all_objects.select_for_update().filter(pk=range_instance_id).first()
        delivery = (
            RangeStatusDelivery.objects.select_for_update()
            .filter(range_instance_id=range_instance_id)
            .order_by("pk")
            .first()
        )
        if delivery is None or delivery.next_attempt_at > timezone.now():
            return False
        try:
            with transaction.atomic():
                range_status_changed.send(
                    sender=None,
                    range_instance_id=delivery.range_instance_id,
                    new_status=delivery.new_status,
                    previous_status=delivery.previous_status,
                    cleanup_verified=delivery.cleanup_verified,
                )
        except Exception as exc:  # noqa: BLE001
            # Receiver errors can contain private data; persist/log the class only.
            delivery.attempts += 1
            delivery.last_error_type = type(exc).__name__[:128]
            delay = min(300, 2 ** min(delivery.attempts, 9))
            delivery.next_attempt_at = timezone.now() + timedelta(seconds=delay)
            delivery.save(update_fields=["attempts", "last_error_type", "next_attempt_at"])
            logger.error(
                "CTF range status delivery retained for retry delivery_id=%s range_instance_id=%s exc_type=%s",
                delivery.pk,
                range_instance_id,
                delivery.last_error_type,
                exc_info=False,
            )
            return False
        delivery.delete()
    return True
