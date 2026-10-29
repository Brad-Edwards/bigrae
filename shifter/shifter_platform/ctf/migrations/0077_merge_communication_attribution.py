"""Join communication-console attribution and the native identity history."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0065_communication_attribution"),
        ("ctf", "0076_event_authority_effect_plan"),
    ]

    operations = []
