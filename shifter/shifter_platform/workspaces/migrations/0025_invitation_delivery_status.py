"""Persist the observed invitation send outcome (ADR-071, #1525).

Rows created before this migration were dispatched fire-and-forget, so their
outcome is unknown; new rows start pending until their send reports.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0024_authorizationoperation_trusted_context"),
    ]

    operations = [
        migrations.AddField(
            model_name="workspaceinvitation",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("failed", "Failed"),
                    ("unavailable", "Unavailable"),
                    ("unknown", "Unknown"),
                ],
                default="unknown",
                help_text="Observed outcome of the send for the current generation; accepted is not mailbox delivery",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="workspaceinvitation",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("failed", "Failed"),
                    ("unavailable", "Unavailable"),
                    ("unknown", "Unknown"),
                ],
                default="pending",
                help_text="Observed outcome of the send for the current generation; accepted is not mailbox delivery",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="workspaceinvitation",
            name="delivery_reason",
            field=models.CharField(blank=True, default="", help_text="Closed transport reason", max_length=32),
        ),
        migrations.AddField(
            model_name="workspaceinvitation",
            name="delivery_observed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name="workspaceinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("delivery_status__in", ["pending", "accepted", "failed", "unavailable", "unknown"])),
                name="workspace_invitation_delivery_status_valid",
            ),
        ),
    ]
