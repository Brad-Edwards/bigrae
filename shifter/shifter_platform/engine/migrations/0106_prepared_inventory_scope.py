"""Prepared inventory retains its application's customer custody."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("engine", "0105_vpn_confirmed_revocation")]
    operations = [
        migrations.AddField(
            "raesimagemapping", "owner_principal_uuid", models.UUIDField(null=True, editable=False, db_index=True)
        ),
        migrations.AddField(
            "raesimagemapping", "scope_kind", models.CharField(max_length=16, blank=True, default="", editable=False)
        ),
        migrations.AddField(
            "raesimagemapping", "account_uuid", models.UUIDField(null=True, editable=False, db_index=True)
        ),
        migrations.AddField(
            "raesimagemapping", "organization_uuid", models.UUIDField(null=True, editable=False, db_index=True)
        ),
        migrations.AddField(
            "raesimagemapping", "workspace_uuid", models.UUIDField(null=True, editable=False, db_index=True)
        ),
        migrations.AddField(
            "raesimagemapping",
            "catalog_publication_scope",
            models.CharField(max_length=16, blank=True, default="", editable=False),
        ),
    ]
