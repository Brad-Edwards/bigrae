"""Pin authenticated session identity even across bulk database writes."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    condition = (
        f"NEW.session_key {comparison} OLD.session_key OR "
        f"NEW.credential_uuid {comparison} OLD.credential_uuid OR "
        f"(OLD.principal_uuid IS NOT NULL AND NEW.principal_uuid {comparison} OLD.principal_uuid)"
    )
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION management_guard_session_reference()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'session reference is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER management_session_reference_immutable
            BEFORE UPDATE ON management_principal_session
            FOR EACH ROW EXECUTE FUNCTION management_guard_session_reference()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER management_session_reference_immutable
            BEFORE UPDATE ON management_principal_session WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'session reference is immutable'); END""")
    else:
        raise RuntimeError("Unsupported session persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS management_session_reference_immutable ON management_principal_session"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS management_guard_session_reference()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS management_session_reference_immutable")


class Migration(migrations.Migration):
    dependencies = [("management", "0019_principal_session_reference")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
