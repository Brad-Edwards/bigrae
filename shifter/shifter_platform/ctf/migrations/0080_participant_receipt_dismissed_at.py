"""Add the participant dismiss timestamp (#2053).

Nullable and not backfilled: no existing fact implies a dismissal.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0079_merge_content_images"),
    ]

    operations = [
        migrations.AddField(
            model_name="participantreceipt",
            name="dismissed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Hidden from the default inbox view; never implies read or acknowledgement",
                null=True,
            ),
        ),
    ]
