"""Encrypt previously parked native Guacamole credentials with the existing key."""

from django.db import migrations


def encrypt_existing_urls(apps, schema_editor):
    from shared.field_encryption import encrypt_value

    Bootstrap = apps.get_model("mission_control", "GuacamoleBootstrapRequest")
    for row in Bootstrap.objects.exclude(result_url="").only("id", "result_url").iterator(chunk_size=200):
        Bootstrap.objects.filter(pk=row.pk).update(result_url=encrypt_value(row.result_url))


class Migration(migrations.Migration):
    dependencies = [("mission_control", "0044_revoke_residual_grants_from_provisioner")]
    # Encryption remains in place on database rollback. Never decrypt secrets
    # merely to restore the old database migration state.
    operations = [migrations.RunPython(encrypt_existing_urls, migrations.RunPython.noop)]
