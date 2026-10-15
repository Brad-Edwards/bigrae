"""Retain revoked identities while allowing an independently bound replacement."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("engine", "0103_vpn_session_crl_guards")]
    operations = [
        migrations.RemoveConstraint("vpnaccessprofile", "engine_vpn_profile_credential_generation"),
        migrations.AddConstraint(
            "vpnaccessprofile",
            models.UniqueConstraint(
                fields=["range", "generation", "resource_generation", "credential_uuid"],
                condition=models.Q(revoked_at__isnull=True),
                name="engine_vpn_profile_credential_generation",
            ),
        ),
    ]
