from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0023_hierarchypublication")]

    operations = [
        migrations.AddField(
            model_name="authorizationoperation",
            name="trusted_context",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
    ]
