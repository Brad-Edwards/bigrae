"""Retain native revision/cancellation actors without guessing legacy authors."""

from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def backfill_first_revision_principal(apps, schema_editor):
    """Only revision one has a proven native creator; other historical actors stay unknown."""
    campaigns = apps.get_model("ctf", "CommunicationCampaign")
    revisions = apps.get_model("ctf", "MessageRevision")
    campaign = campaigns.objects.filter(pk=OuterRef("campaign_id"))
    revisions.objects.filter(revision_number=1, actor_principal_uuid__isnull=True).update(
        actor_principal_uuid=Subquery(campaign.values("creator_principal_uuid")[:1]),
    )


class Migration(migrations.Migration):
    dependencies = [("ctf", "0077_merge_communication_attribution")]

    operations = [
        migrations.AddField(
            model_name="communicationcampaign",
            name="cancelled_by_principal_uuid",
            field=models.UUIDField(null=True, blank=True, editable=False),
        ),
        migrations.AddField(
            model_name="messagerevision",
            name="actor_principal_uuid",
            field=models.UUIDField(null=True, blank=True, editable=False),
        ),
        migrations.RunPython(backfill_first_revision_principal, migrations.RunPython.noop),
    ]
