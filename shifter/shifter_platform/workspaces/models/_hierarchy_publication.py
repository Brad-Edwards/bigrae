"""Durable publication state for SQL-owned authorization hierarchy edges."""

import uuid

from django.db import models


class HierarchyPublication(models.Model):
    """Exact parent intent and verified provider outcome for one hierarchy object."""

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested"
        CONFIRMED = "confirmed", "Confirmed"
        UNRESOLVED = "unresolved", "Unresolved"

    class ObjectKind(models.TextChoices):
        ACCOUNT = "account", "Account"
        ORGANIZATION = "organization", "Organization"
        WORKSPACE = "workspace", "Workspace"

    class ParentKind(models.TextChoices):
        INSTALLATION = "installation", "Installation"
        ACCOUNT = "account", "Account"
        ORGANIZATION = "organization", "Organization"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    object_kind = models.CharField(max_length=16, choices=ObjectKind.choices)
    object_uuid = models.UUIDField()
    parent_kind = models.CharField(max_length=16, choices=ParentKind.choices)
    parent_uuid = models.UUIDField(null=True, blank=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.REQUESTED, db_index=True)
    provider_store_id = models.CharField(max_length=64, blank=True, default="")
    model_id = models.CharField(max_length=64, blank=True, default="")
    outcome_reason = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_hierarchy_publication"
        constraints = [
            models.UniqueConstraint(
                fields=["object_kind", "object_uuid"],
                name="uniq_hierarchy_publication_object",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(object_kind="account", parent_kind="installation", parent_uuid__isnull=True)
                    | models.Q(object_kind="organization", parent_kind="account", parent_uuid__isnull=False)
                    | models.Q(object_kind="workspace", parent_kind="organization", parent_uuid__isnull=False)
                ),
                name="hierarchy_publication_shape",
            ),
            models.CheckConstraint(
                condition=(~models.Q(state="confirmed") | (~models.Q(provider_store_id="") & ~models.Q(model_id=""))),
                name="hierarchy_publication_confirmed_binding",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.object_kind}:{self.object_uuid} ({self.state})"
