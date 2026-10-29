"""Preserve original launch authority and prevent cancelled intent resurrection."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    model = apps.get_model("ctf", "CTFRangeLaunch")
    mutable = {"updated_at", "range_instance_id", "dispatched_at", "deleted_at"}
    terms = [
        f"NEW.{field.column} {comparison} OLD.{field.column}"
        for field in model._meta.local_fields
        if field.name not in mutable
    ]
    terms.extend(
        f"(OLD.{name} IS NOT NULL AND NEW.{name} {comparison} OLD.{name})"
        for name in ("range_instance_id", "dispatched_at", "deleted_at")
    )
    changed = " OR ".join(terms)
    table = schema_editor.quote_name(model._meta.db_table)
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION ctf_guard_range_launch_intent()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {changed} THEN
                RAISE EXCEPTION 'range launch intent is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute(f"""CREATE TRIGGER ctf_range_launch_intent_immutable
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ctf_guard_range_launch_intent()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER ctf_range_launch_intent_immutable
            BEFORE UPDATE ON {table} WHEN {changed}
            BEGIN SELECT RAISE(ABORT, 'range launch intent is immutable'); END""")
    else:
        raise RuntimeError("Unsupported launch intent persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS ctf_range_launch_intent_immutable ON ctf_ctfrangelaunch")
        schema_editor.execute("DROP FUNCTION IF EXISTS ctf_guard_range_launch_intent()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS ctf_range_launch_intent_immutable")


class Migration(migrations.Migration):
    dependencies = [("ctf", "0074_ctfparticipant_range_request_id_ctfrangelaunch")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
