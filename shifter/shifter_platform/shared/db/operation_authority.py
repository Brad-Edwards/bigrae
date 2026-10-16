"""Scalar admission facts on existing durable operations (ADR-066-R8).

This adds fields only. Each owning operation keeps its own generation, claims,
dispatch, cancellation and cleanup state machines; there is no shared job store.
"""

from django.db import models


class RecordedAuthorityFields(models.Model):
    """Separate immutable initiating proof from business owner and workload."""

    authority_kind = models.CharField(max_length=16, blank=True, default="", editable=False)
    authority_action = models.CharField(max_length=100, blank=True, default="", editable=False)
    authority_target_type = models.CharField(max_length=16, blank=True, default="", editable=False)
    authority_target_uuid = models.UUIDField(null=True, editable=False)
    authority_parent_type = models.CharField(max_length=16, blank=True, default="", editable=False)
    authority_parent_uuid = models.UUIDField(null=True, editable=False)
    authority_scope_kind = models.CharField(max_length=16, blank=True, default="", editable=False)
    authority_account_uuid = models.UUIDField(null=True, editable=False)
    authority_organization_uuid = models.UUIDField(null=True, editable=False)
    authority_workspace_uuid = models.UUIDField(null=True, editable=False)
    authority_owner_uuid = models.UUIDField(null=True, editable=False)
    authority_resource_uuid = models.UUIDField(null=True, editable=False)
    authority_binding_digest = models.CharField(max_length=71, blank=True, default="", editable=False)
    initiating_principal_uuid = models.UUIDField(null=True, editable=False, db_index=True)
    initiating_principal_kind = models.CharField(max_length=8, blank=True, default="", editable=False)
    credential_kind = models.CharField(max_length=16, blank=True, default="", editable=False)
    credential_uuid = models.UUIDField(null=True, editable=False, db_index=True)
    credential_ceiling_actions = models.TextField(blank=True, default="", editable=False)
    credential_target_type = models.CharField(max_length=16, blank=True, default="", editable=False)
    credential_target_uuid = models.UUIDField(null=True, editable=False)
    credential_scopes = models.TextField(blank=True, default="", editable=False)
    credential_event_uuid = models.UUIDField(null=True, editable=False)
    executing_principal_uuid = models.UUIDField(null=True, editable=False)

    class Meta:
        """Declare persistence tables, indexes and custody constraints."""

        abstract = True
