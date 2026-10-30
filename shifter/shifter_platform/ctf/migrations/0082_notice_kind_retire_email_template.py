"""Record the campaign notice kind and retire email-template overrides (#580).

Existing campaigns get ``announcement``: their kind was never stored, so it is
not guessed. The per-event override table is dropped; no sender read it.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0081_communication_delivery_state"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="ctfemailtemplate",
            name="unique_active_email_template_per_event_type",
        ),
        migrations.AddField(
            model_name="communicationcampaign",
            name="notice_kind",
            field=models.CharField(
                choices=[
                    ("announcement", "Announcement"),
                    ("reminder", "Reminder"),
                    ("cleanup", "Cleanup"),
                    ("results", "Results"),
                    ("range_ready", "Range Ready"),
                    ("range_failure", "Range Failure"),
                ],
                default="announcement",
                editable=False,
                help_text="Selects the trusted email template pair; set once at creation, never authority (#580)",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="communicationcampaign",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "notice_kind__in",
                        ["announcement", "reminder", "cleanup", "results", "range_ready", "range_failure"],
                    )
                ),
                name="ctf_comm_campaign_notice_kind_valid",
            ),
        ),
        migrations.DeleteModel(
            name="CTFEmailTemplate",
        ),
    ]
