"""Native upload admission, immutable storage identity and quota reservation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import models

from shared.db import RecordedAuthorityFields

if TYPE_CHECKING:
    pass


class AgentUploadBudget(models.Model):
    """Serialize admission for one actual customer scope, including empty scopes."""

    scope_digest = models.CharField(max_length=71, primary_key=True, editable=False)

    def __str__(self) -> str:
        return self.scope_digest


class AgentUpload(RecordedAuthorityFields):
    """Reference-only record behind the incumbent signed upload token."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    scope_digest = models.CharField(max_length=71, editable=False, db_index=True)
    name = models.CharField(max_length=100, editable=False)
    filename = models.CharField(max_length=255, editable=False)
    os_slug = models.CharField(max_length=100, editable=False)
    agent_type = models.CharField(max_length=30, editable=False)
    file_size = models.PositiveBigIntegerField(editable=False)
    storage_provider = models.CharField(max_length=16, editable=False)
    storage_bucket = models.CharField(max_length=255, editable=False)
    staging_key = models.CharField(max_length=500, unique=True, editable=False)
    install_key = models.CharField(max_length=500, unique=True, editable=False)
    expires_at = models.DateTimeField(editable=False)
    state = models.CharField(max_length=16, default="issued", editable=False, db_index=True)
    cleanup_pending = models.BooleanField(default=True, editable=False)
    validated_identity = models.JSONField(default=dict, editable=False)
    agent = models.ForeignKey("cms.AgentConfig", null=True, on_delete=models.PROTECT, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Declare persistence tables, indexes and custody constraints."""

        constraints = [
            models.CheckConstraint(condition=models.Q(file_size__gt=0), name="agent_upload_positive_size"),
            models.CheckConstraint(
                condition=models.Q(state__in=("issued", "finalizing", "completed", "cancelled")),
                name="agent_upload_valid_state",
            ),
        ]
