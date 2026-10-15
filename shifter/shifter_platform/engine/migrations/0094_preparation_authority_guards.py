"""Keep original preparation authority immutable through retry and cleanup."""

from django.db import migrations

_FIELDS = (
    "id",
    "adapter_id",
    "requested_by_id",
    "scope_digest",
    "input_digest",
    "input",
    "authority_kind",
    "authority_action",
    "authority_target_type",
    "authority_target_uuid",
    "authority_parent_type",
    "authority_parent_uuid",
    "authority_scope_kind",
    "authority_account_uuid",
    "authority_organization_uuid",
    "authority_workspace_uuid",
    "authority_owner_uuid",
    "authority_resource_uuid",
    "authority_binding_digest",
    "initiating_principal_uuid",
    "initiating_principal_kind",
    "credential_kind",
    "credential_uuid",
    "credential_ceiling_actions",
    "credential_target_type",
    "credential_target_uuid",
    "credential_scopes",
    "credential_event_uuid",
)


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    changed = " OR ".join(f"NEW.{field} {comparison} OLD.{field}" for field in _FIELDS)
    condition = f"OLD.authority_kind <> '' AND ({changed})"
    condition += (
        " OR (OLD.executing_principal_uuid IS NOT NULL AND "
        f"NEW.executing_principal_uuid {comparison} OLD.executing_principal_uuid)"
    )
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_preparation_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'preparation authority is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_preparation_authority_immutable
            BEFORE UPDATE ON engine_preparationoperation
            FOR EACH ROW EXECUTE FUNCTION engine_guard_preparation_authority()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_preparation_authority_immutable
            BEFORE UPDATE ON engine_preparationoperation WHEN {condition}
            BEGIN SELECT RAISE(ABORT, 'preparation authority is immutable'); END""")
    else:
        raise RuntimeError("Unsupported preparation persistence backend")


def remove_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS engine_preparation_authority_immutable ON engine_preparationoperation"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_preparation_authority()")
    elif schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS engine_preparation_authority_immutable")


class Migration(migrations.Migration):
    dependencies = [("engine", "0093_preparation_initiating_authority")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
