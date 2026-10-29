"""Durable global state for the coordinated identity and policy cutover."""

from django.db import models


class IdentityCutoverState(models.Model):
    """Singleton state that prevents a partial SQL/OpenFGA cutover from starting."""

    class State(models.TextChoices):
        """Closed cutover phases."""

        PLANNED = "planned", "Planned"
        SQL_APPLIED = "sql_applied", "SQL applied"
        POLICY_PENDING = "policy_pending", "Policy pending"
        VERIFIED = "verified", "Verified"
        ACTIVATED = "activated", "Activated"

    key = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    state = models.CharField(max_length=16, choices=State.choices)
    plan_digest = models.CharField(max_length=64)
    plan_payload = models.JSONField(default=dict)
    relationship_intent = models.JSONField(default=list)
    tuple_digest = models.CharField(max_length=64, blank=True, default="")
    provider_store_id = models.CharField(max_length=64, blank=True, default="")
    model_id = models.CharField(max_length=64, blank=True, default="")
    bootstrap_principal_uuid = models.UUIDField(null=True, blank=True)
    bootstrap_verified_at = models.DateTimeField(null=True, blank=True)
    admission_invalidated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Enforce one row and complete provider binding at activation."""

        db_table = "workspaces_identity_cutover_state"
        constraints = [
            models.CheckConstraint(condition=models.Q(key=1), name="identity_cutover_singleton"),
            models.CheckConstraint(
                condition=(
                    ~models.Q(state="activated")
                    | (
                        ~models.Q(tuple_digest="")
                        & ~models.Q(provider_store_id="")
                        & ~models.Q(model_id="")
                        & models.Q(bootstrap_principal_uuid__isnull=False)
                        & models.Q(bootstrap_verified_at__isnull=False)
                        & models.Q(admission_invalidated_at__isnull=False)
                    )
                ),
                name="identity_cutover_activated_binding",
            ),
        ]

    def __str__(self) -> str:
        return f"identity-cutover:{self.state}"
