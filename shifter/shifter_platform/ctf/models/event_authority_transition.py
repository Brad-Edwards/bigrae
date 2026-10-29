"""Durable intent for event staff and ownership authority changes."""

from __future__ import annotations

from uuid import uuid4

from django.db import models

from .event import CTFEvent


class CTFEventAuthorityTransition(models.Model):
    """Serialize one event authority change across SQL and the policy provider.

    The row is created while the event is locked, before any provider write.
    Provider calls then use this row's UUID as their stable idempotency key.
    A failed call leaves a resumable pending row instead of an unrecorded
    provider-side partial change.
    """

    class Kind(models.TextChoices):
        """Closed kinds of retained event authority transitions."""

        ASSIGN = "assign", "Assign staff"
        REVOKE = "revoke", "Revoke staff"
        TRANSFER = "transfer", "Transfer ownership"

    class State(models.TextChoices):
        """Publication progress for an immutable event authority plan."""

        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(CTFEvent, on_delete=models.CASCADE, related_name="authority_transitions")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING, db_index=True)
    actor_principal_uuid = models.UUIDField(editable=False)
    actor_credential_uuid = models.UUIDField(editable=False)
    target_principal_uuid = models.UUIDField(editable=False)
    previous_owner_principal_uuid = models.UUIDField(null=True, blank=True, editable=False)
    previous_role = models.CharField(max_length=16, blank=True, default="", editable=False)
    requested_role = models.CharField(max_length=16, blank=True, default="", editable=False)
    effect_plan = models.JSONField(default=dict, editable=False)
    effect_plan_digest = models.CharField(max_length=64, default="", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        """Persist transition identity, ordering and pending-plan constraints."""

        db_table = "ctf_event_authority_transition"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(state="pending") | ~models.Q(effect_plan_digest=""),
                name="ctf_pending_authority_has_effect_plan",
            ),
            models.UniqueConstraint(
                fields=["event"],
                condition=models.Q(state="pending"),
                name="unique_pending_ctf_event_authority_transition",
            ),
        ]

    def __str__(self) -> str:
        """Return a compact operator-facing transition identity."""
        return f"{self.kind}:{self.event_id}:{self.target_principal_uuid}:{self.state}"
