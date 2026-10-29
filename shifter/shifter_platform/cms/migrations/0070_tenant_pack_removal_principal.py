import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cms", "0069_rangeinstance_launch_intent_digest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="tenantpackremoval",
            name="upload_outcome",
            field=models.CharField(
                blank=True,
                choices=[("", "Removal"), ("unknown", "Unknown"), ("settled", "Settled")],
                default="",
                max_length=8,
            ),
        ),
        migrations.AddConstraint(
            model_name="tenantpackremoval",
            constraint=models.CheckConstraint(
                condition=models.Q(upload_outcome__in=["", "unknown", "settled"]), name="tenant_pack_upload_outcome"
            ),
        ),
        migrations.AlterField(
            model_name="tenantpackremoval",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="tenantpackremoval",
            name="requested_by_principal_uuid",
            field=models.UUIDField(db_index=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="scenariomodelneeds",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="scenariomodelneeds",
            name="updated_by_principal_uuid",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
    ]
