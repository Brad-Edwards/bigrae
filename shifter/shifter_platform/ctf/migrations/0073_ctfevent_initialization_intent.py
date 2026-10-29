"""Keep event admission fenced until requested initialization commits."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ctf", "0072_ctfeventauthoritytransition")]

    operations = [
        migrations.AddField(
            model_name="ctfevent",
            name="initialization_intent",
            field=models.JSONField(default=None, editable=False, null=True),
        ),
    ]
