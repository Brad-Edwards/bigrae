"""Protect the installed provisioner identity independently from application authority."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    condition = f"OLD.authority_kind <> '' AND NEW.executing_workload {comparison} OLD.executing_workload"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_launch_workload()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN
                RAISE EXCEPTION 'launch workload is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_launch_workload_immutable
            BEFORE UPDATE ON engine_provisioner_launch_intent
            FOR EACH ROW EXECUTE FUNCTION engine_guard_launch_workload()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_launch_workload_immutable
            BEFORE UPDATE ON engine_provisioner_launch_intent WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'launch workload is immutable'); END""")
    else:
        raise RuntimeError("Unsupported launch workload persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS engine_launch_workload_immutable ON engine_provisioner_launch_intent"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_launch_workload()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_launch_workload_immutable")


class Migration(migrations.Migration):
    dependencies = [("engine", "0115_launch_executing_workload")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
