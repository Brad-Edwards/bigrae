"""Encrypt retained nested registration data, including retired CMS devices."""

from django.db import migrations


def encrypt_retained_specs(apps, schema_editor):
    from shared.field_encryption import _transform_sensitive

    instance = apps.get_model("cms", "Instance")
    rows = instance._base_manager.using(schema_editor.connection.alias)
    for identifier, data in rows.values_list("pk", "data").iterator(chunk_size=250):
        encrypted = _transform_sensitive(data, frozenset({"authcode", "scm_pin_value", "otp_value"}), encrypt=True)
        if encrypted != data:
            rows.filter(pk=identifier).update(data=encrypted)


class Migration(migrations.Migration):
    dependencies = [("cms", "0064_device_custody_guards")]
    operations = [migrations.RunPython(encrypt_retained_specs, migrations.RunPython.noop)]
