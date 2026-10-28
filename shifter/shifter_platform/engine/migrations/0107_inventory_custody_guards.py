"""Prevent prepared inventory from moving between customer scopes."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    fields = ("owner_principal_uuid", "scope_kind", "account_uuid", "organization_uuid", "workspace_uuid")
    changed = " OR ".join(f"NEW.{field} {comparison} OLD.{field}" for field in fields)
    condition = f"OLD.scope_kind <> '' AND ({changed})"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_inventory_custody()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'inventory custody is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_inventory_custody_immutable
            BEFORE UPDATE ON engine_raes_image_mapping
            FOR EACH ROW EXECUTE FUNCTION engine_guard_inventory_custody()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_inventory_custody_immutable
            BEFORE UPDATE ON engine_raes_image_mapping WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'inventory custody is immutable'); END""")
    else:
        raise RuntimeError("Unsupported inventory persistence backend")


def remove_guard(apps, schema_editor):
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS engine_inventory_custody_immutable"
        + (" ON engine_raes_image_mapping" if schema_editor.connection.vendor == "postgresql" else "")
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_inventory_custody()")


class Migration(migrations.Migration):
    dependencies = [("engine", "0106_prepared_inventory_scope")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
