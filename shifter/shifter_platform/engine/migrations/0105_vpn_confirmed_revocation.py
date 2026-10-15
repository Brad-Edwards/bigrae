"""Keep accepted revocation evidence before deleting private profile material."""

from django.db import migrations, models


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    condition = " OR ".join(
        f"(OLD.{name} IS NOT NULL AND NEW.{name} {comparison} OLD.{name})"
        for name in ("revocation_operation_id", "secret_removed_at")
    )
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_vpn_revocation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'VPN revocation evidence is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_vpn_revocation_immutable
            BEFORE UPDATE ON engine_vpn_access_profile
            FOR EACH ROW EXECUTE FUNCTION engine_guard_vpn_revocation()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_vpn_revocation_immutable
            BEFORE UPDATE ON engine_vpn_access_profile
            WHEN {condition} BEGIN SELECT RAISE(ABORT, 'VPN revocation evidence is immutable'); END""")
    else:
        raise RuntimeError("Unsupported VPN persistence backend")


def remove_guard(apps, schema_editor):
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS engine_vpn_revocation_immutable"
        + (" ON engine_vpn_access_profile" if schema_editor.connection.vendor == "postgresql" else "")
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_vpn_revocation()")


class Migration(migrations.Migration):
    dependencies = [("engine", "0104_vpn_active_profile_uniqueness")]
    operations = [
        migrations.AddField("vpnaccessprofile", "revocation_operation_id", models.UUIDField(null=True, editable=False)),
        migrations.AddField("vpnaccessprofile", "secret_removed_at", models.DateTimeField(null=True, editable=False)),
        migrations.RunPython(install_guard, remove_guard),
    ]
