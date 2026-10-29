"""Durable, ordered delivery of CMS status changes to local CTF receivers."""

from django.db import models
from django.utils import timezone


class RangeStatusDelivery(models.Model):
    """Pending signal arguments; removed atomically with successful receiver writes.

    The source is a scalar rather than a cascading FK: pruning the CMS projection
    must not discard a pending capacity release or participant unlink.
    """

    range_instance_id = models.PositiveBigIntegerField(db_index=True)
    new_status = models.CharField(max_length=20)
    previous_status = models.CharField(max_length=20)
    cleanup_verified = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_error_type = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return only non-secret delivery identity and status."""
        return f"Range status delivery {self.pk}: {self.new_status}"
