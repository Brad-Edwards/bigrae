"""Accepted scope/owner change provenance for generation-bound native cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

from uuid import uuid4

from django.db import models

from shared.db import RecordedAuthorityFields


class RangeAuthorityChange(RecordedAuthorityFields):
    """Immutable admission evidence; delivery remains in the incumbent outbox."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    range = models.ForeignKey("engine.Range", on_delete=models.PROTECT, related_name="authority_changes")
    request_uuid = models.UUIDField(editable=False)
    resource = models.CharField(max_length=16, editable=False)
    operation = models.CharField(max_length=16, editable=False)
    resource_generation = models.UUIDField(editable=False)
    previous_binding_digest = models.CharField(max_length=71, editable=False)
    accepted_at = models.DateTimeField(auto_now_add=True)

    @property
    def operation_id(self) -> UUID:
        return self.id

    @property
    def payload(self) -> dict[str, object]:
        return {"resource": self.resource, "operation": self.operation, "request_id": str(self.request_uuid)}

    class Meta:
        """Declare persistence tables, indexes and custody constraints."""

        db_table = "engine_range_authority_change"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(resource__in=["range", "raes-range"]), name="range_change_resource"
            ),
            models.CheckConstraint(condition=models.Q(operation__in=["owner", "scope"]), name="range_change_operation"),
        ]
