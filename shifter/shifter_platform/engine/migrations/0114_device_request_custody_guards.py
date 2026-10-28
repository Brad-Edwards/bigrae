"""Retain independent native device custody through bulk writes and retries."""

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
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_device_custody()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN
                RAISE EXCEPTION 'device custody is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_device_custody_immutable
            BEFORE UPDATE ON engine_request FOR EACH ROW EXECUTE FUNCTION engine_guard_device_custody()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_device_custody_immutable
            BEFORE UPDATE ON engine_request WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'device custody is immutable'); END""")
    else:
        raise RuntimeError("Unsupported device custody persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_device_custody_immutable ON engine_request")
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_device_custody()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_device_custody_immutable")


def encrypt_native_specs(apps, schema_editor):
    from engine.field_encryption import transform_spec

    alias = schema_editor.connection.alias
    for model_name in ("Instance", "App"):
        model = apps.get_model("engine", model_name)
        selector = {"role": "ngfw"} if model_name == "Instance" else {"app_type": "ngfw"}
        for row in model.objects.using(alias).filter(**selector).only("pk", "spec").iterator(chunk_size=200):
            encrypted = transform_spec(row.spec, encrypt=True)
            if encrypted != row.spec:
                model.objects.using(alias).filter(pk=row.pk).update(spec=encrypted)


class Migration(migrations.Migration):
    dependencies = [("engine", "0113_device_request_scope")]
    operations = [
        migrations.RunPython(encrypt_native_specs, migrations.RunPython.noop),
        migrations.RunPython(install_guard, remove_guard),
    ]
