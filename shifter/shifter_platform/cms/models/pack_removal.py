"""Tenant pack archive inventory and durable removal records (ADR-034-R13, #2520)."""

from __future__ import annotations

from uuid import uuid4

from django.conf import settings
from django.db import models


class TenantPackRemoval(models.Model):
    """A committed tenant pack removal and its resumable storage cleanup.

    The catalog row is hard-deleted when this record commits. The record keeps
    the removed identity for audit correlation and drives retries until every
    captured archive object is absent.
    """

    class State(models.TextChoices):
        """Cleanup progress of the removal."""

        PENDING = "pending", "Cleanup pending"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    organization_uuid = models.UUIDField(db_index=True, editable=False)
    scenario_id = models.SlugField(max_length=100, db_index=True, editable=False)
    package_name = models.SlugField(max_length=100, editable=False)
    package_version = models.CharField(max_length=128, editable=False)
    package_digest = models.CharField(max_length=71, editable=False)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )
    requested_by_principal_uuid = models.UUIDField(null=True, editable=False, db_index=True)
    request_id = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(db_index=True)
    lease_id = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_failure_code = models.CharField(max_length=32, blank=True, default="")
    # An unknown upload may still finish after a transport failure or process
    # crash. Its exact object remains cleanup-owned even after an absent read.
    upload_outcome = models.CharField(
        max_length=8, blank=True, default="", choices=[("", "Removal"), ("unknown", "Unknown"), ("settled", "Settled")]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Model options: legal states and the removal history ordering."""

        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=["pending", "completed"]), name="tenant_pack_removal_state"
            ),
            models.CheckConstraint(
                condition=models.Q(upload_outcome__in=["", "unknown", "settled"]), name="tenant_pack_upload_outcome"
            ),
        ]

    def __str__(self) -> str:
        return f"Tenant pack removal {self.id} ({self.state})"


class TenantPackArchive(models.Model):
    """One stored archive object owned by a tenant pack registration.

    Every upload revision records its exact object here in the registration
    transaction. A removal captures the pack's rows and deletes each row once its
    object is absent. Legacy rows from the backfill carry only ``package_ref``;
    the removal binds a candidate location, and cleanup must observe the object
    there before deleting, or an operator must relocate or confirm it.
    """

    organization_uuid = models.UUIDField(db_index=True, editable=False)
    scenario_id = models.SlugField(max_length=100, db_index=True, editable=False)
    package_ref = models.CharField(max_length=512, editable=False)
    bucket = models.CharField(max_length=255, blank=True, default="")
    object_key = models.CharField(max_length=1024, blank=True, default="")
    #: True for an exact server-generated destination or an observed legacy location.
    #: An unverified location never lets absence count as deletion.
    location_verified = models.BooleanField(default=False)
    removal = models.ForeignKey(
        TenantPackRemoval, null=True, blank=True, on_delete=models.PROTECT, related_name="archives"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model options: one inventory row per owned reference."""

        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["package_ref"], name="tenant_pack_archive_ref_unique"),
        ]

    def __str__(self) -> str:
        return f"Tenant pack archive {self.pk}"
