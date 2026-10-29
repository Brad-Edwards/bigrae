"""Join native identity attribution with first-party content-image history."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0078_native_communication_attribution"),
        ("ctf", "0067_backfill_content_images"),
    ]

    operations = []
