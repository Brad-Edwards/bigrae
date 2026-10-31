"""Widen provider subjects to hold an AWS IAM role ARN (#2313).

PostgreSQL widens the column in place and keeps the immutability trigger.
SQLite rebuilds the table, which drops its triggers, so this migration removes
and installs them again around the change in both directions.
"""

from django.db import migrations, models

_SQLITE_GUARDS = (
    """
    CREATE TRIGGER management_provider_binding_immutable_update
    BEFORE UPDATE ON management_provider_binding
    BEGIN SELECT RAISE(ABORT, 'provider binding is immutable'); END
    """,
    """
    CREATE TRIGGER management_provider_binding_immutable_delete
    BEFORE DELETE ON management_provider_binding
    BEGIN SELECT RAISE(ABORT, 'provider binding is immutable'); END
    """,
)


def install_sqlite_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for statement in _SQLITE_GUARDS:
            schema_editor.execute(statement)


def remove_sqlite_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS management_provider_binding_immutable_update")
        schema_editor.execute("DROP TRIGGER IF EXISTS management_provider_binding_immutable_delete")


class Migration(migrations.Migration):
    dependencies = [("management", "0020_session_reference_guards")]

    operations = [
        migrations.RunPython(remove_sqlite_guard, install_sqlite_guard),
        migrations.AlterField(
            model_name="providerbinding",
            name="subject",
            field=models.CharField(max_length=2048),
        ),
        migrations.RunPython(install_sqlite_guard, remove_sqlite_guard),
    ]
