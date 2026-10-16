"""Accepted cancellation proof cannot be replaced during interrupt convergence."""

from django.db import migrations


def install_guard(apps, schema_editor):
    table = "engine_range_cancellation_authority"
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    model = apps.get_model("engine", "RangeCancellationAuthority")
    changed = " OR ".join(
        f"NEW.{field.column} {comparison} OLD.{field.column}"
        for field in model._meta.local_fields
        if field.name != "executing_principal_uuid"
    )
    changed += (
        " OR (OLD.executing_principal_uuid IS NOT NULL AND "
        f"NEW.executing_principal_uuid {comparison} OLD.executing_principal_uuid)"
    )
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_cancellation_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {changed} THEN
                RAISE EXCEPTION 'cancellation authority is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute(f"""CREATE TRIGGER engine_cancellation_authority_immutable
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION engine_guard_cancellation_authority()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_cancellation_authority_immutable
            BEFORE UPDATE ON {table} WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'cancellation authority is immutable'); END""")
    else:
        raise RuntimeError("Unsupported cancellation persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS engine_cancellation_authority_immutable ON engine_range_cancellation_authority"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_cancellation_authority()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_cancellation_authority_immutable")


class Migration(migrations.Migration):
    dependencies = [("engine", "0109_range_cancellation_authority")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
