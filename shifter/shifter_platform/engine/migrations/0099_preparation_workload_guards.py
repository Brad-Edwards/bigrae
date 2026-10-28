"""Preserve source attempt identity and append-only receipts across bulk writes."""

from django.db import migrations

_FIELDS = ("id", "operation_id", "phase", "input", "input_digest", "expires_at", "executing_workload")


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    empty = "'{}'::jsonb" if vendor == "postgresql" else "'{}'"
    changed = " OR ".join(f"NEW.{field} {comparison} OLD.{field}" for field in _FIELDS)
    receipt_changed = " OR ".join(f"NEW.{field} {comparison} OLD.{field}" for field in ("result", "result_digest"))
    condition = f"OLD.executing_workload <> {empty} AND (({changed}) OR "
    condition += f"(OLD.result_digest <> '' AND ({receipt_changed})))"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_preparation_workload()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'preparation workload is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_preparation_workload_immutable
            BEFORE UPDATE ON engine_preparationattempt
            FOR EACH ROW EXECUTE FUNCTION engine_guard_preparation_workload()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_preparation_workload_immutable
            BEFORE UPDATE ON engine_preparationattempt WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'preparation workload is immutable'); END""")
    else:
        raise RuntimeError("Unsupported preparation persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_preparation_workload_immutable ON engine_preparationattempt")
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_preparation_workload()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_preparation_workload_immutable")


class Migration(migrations.Migration):
    dependencies = [("engine", "0098_preparation_executing_workload")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
