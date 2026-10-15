"""Protect accepted transfer proof and cleanup custody through native retries."""

from django.db import migrations


def install_guard(apps, schema_editor):
    table = "engine_range_authority_change"
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    model = apps.get_model("engine", "RangeAuthorityChange")
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
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_range_change_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'range change authority is retained' USING ERRCODE = '23514';
            END IF;
            IF {changed} THEN
                RAISE EXCEPTION 'range change authority is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute(f"""CREATE TRIGGER engine_range_change_authority_immutable
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION engine_guard_range_change_authority()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_range_change_authority_immutable
            BEFORE UPDATE ON {table} WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'range change authority is immutable'); END""")
        schema_editor.execute(f"""CREATE TRIGGER engine_range_change_authority_retained
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'range change authority is retained'); END""")
    else:
        raise RuntimeError("Unsupported range authority persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS engine_range_change_authority_immutable ON engine_range_authority_change"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_range_change_authority()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_range_change_authority_immutable")
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_range_change_authority_retained")


class Migration(migrations.Migration):
    dependencies = [("engine", "0111_range_authority_changes")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
