"""Protect native upload proof and storage identity, including bulk writes."""

from django.db import migrations

_INPUT = (
    "id",
    "scope_digest",
    "name",
    "filename",
    "os_slug",
    "agent_type",
    "file_size",
    "storage_provider",
    "storage_bucket",
    "staging_key",
    "install_key",
    "expires_at",
    "authority_kind",
    "authority_action",
    "authority_target_type",
    "authority_target_uuid",
    "authority_scope_kind",
    "authority_account_uuid",
    "authority_organization_uuid",
    "authority_workspace_uuid",
    "authority_owner_uuid",
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
    compare = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    changed = " OR ".join(f"NEW.{field} {compare} OLD.{field}" for field in _INPUT)
    evidence = f"(OLD.validated_identity <> '{{}}' AND NEW.validated_identity {compare} OLD.validated_identity)"
    admitted = f"(OLD.agent_id IS NOT NULL AND NEW.agent_id {compare} OLD.agent_id)"
    condition = f"({changed}) OR {evidence} OR {admitted}"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION cms_guard_upload_custody()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF {condition} THEN RAISE EXCEPTION 'upload custody is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER cms_upload_custody_immutable BEFORE UPDATE ON cms_agentupload
            FOR EACH ROW EXECUTE FUNCTION cms_guard_upload_custody()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER cms_upload_custody_immutable BEFORE UPDATE ON cms_agentupload
            WHEN {condition} BEGIN SELECT RAISE(ABORT, 'upload custody is immutable'); END""")
    else:
        raise RuntimeError("Unsupported upload persistence backend")


def remove_guard(apps, schema_editor):
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS cms_upload_custody_immutable"
        + (" ON cms_agentupload" if schema_editor.connection.vendor == "postgresql" else "")
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS cms_guard_upload_custody()")


class Migration(migrations.Migration):
    dependencies = [("cms", "0059_upload_object_identity")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
