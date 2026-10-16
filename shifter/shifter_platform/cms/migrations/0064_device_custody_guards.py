"""Protect CMS device request custody, including updates bypassing the services."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    fields = (
        "request_id",
        "request_type",
        "user_id",
        "owner_principal_uuid",
        "scope_kind",
        "account_id",
        "organization_id",
        "workspace_id",
    )
    changed = " OR ".join(f"NEW.{name} {comparison} OLD.{name}" for name in fields)
    condition = f"OLD.owner_principal_uuid IS NOT NULL AND OLD.request_type = 'ngfw' AND ({changed})"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION cms_guard_device_custody()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN
                RAISE EXCEPTION 'device custody is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER cms_device_custody_immutable
            BEFORE UPDATE ON cms_request FOR EACH ROW EXECUTE FUNCTION cms_guard_device_custody()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER cms_device_custody_immutable
            BEFORE UPDATE ON cms_request WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'device custody is immutable'); END""")
    else:
        raise RuntimeError("Unsupported device custody persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS cms_device_custody_immutable ON cms_request")
        schema_editor.execute("DROP FUNCTION IF EXISTS cms_guard_device_custody()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS cms_device_custody_immutable")


class Migration(migrations.Migration):
    dependencies = [("cms", "0063_scope_device_admission")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
