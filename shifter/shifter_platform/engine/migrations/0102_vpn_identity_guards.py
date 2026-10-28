"""Keep certificate custody and initiating credential immutable across bulk writes."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    mutable = {
        "state",
        "certificate_serial",
        "certificate_fingerprint",
        "issuer_fingerprint",
        "secret_ref",
        "provisioner_operation_id",
        "provisioner_operation",
        "issuance_operation_id",
        "revoked_at",
        "updated_at",
    }
    columns = [
        field.column for field in apps.get_model("engine", "VpnAccessProfile")._meta.fields if field.name not in mutable
    ]
    changes = " OR ".join(f"NEW.{column} {comparison} OLD.{column}" for column in columns)
    receipt = " OR ".join(
        f"NEW.{column} {comparison} OLD.{column}"
        for column in ("certificate_serial", "certificate_fingerprint", "issuer_fingerprint", "secret_ref")
    )
    condition = f"({changes}) OR (OLD.certificate_serial <> '' AND ({receipt}))"
    condition += " OR (OLD.issuance_operation_id IS NOT NULL AND "
    condition += f"NEW.issuance_operation_id {comparison} OLD.issuance_operation_id)"
    condition += f" OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at {comparison} OLD.revoked_at)"
    condition += " OR (OLD.state = 'revoked' AND NEW.state <> 'revoked')"
    if vendor == "postgresql":
        schema_editor.execute(f"""CREATE FUNCTION engine_guard_vpn_identity() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN
                RAISE EXCEPTION 'VPN profile identity is immutable' USING ERRCODE = '23514';
            END IF; RETURN NEW; END; $$""")
        schema_editor.execute("""CREATE TRIGGER engine_vpn_identity_immutable BEFORE UPDATE ON engine_vpn_access_profile
            FOR EACH ROW EXECUTE FUNCTION engine_guard_vpn_identity()""")
    elif vendor == "sqlite":
        schema_editor.execute(f"""CREATE TRIGGER engine_vpn_identity_immutable
            BEFORE UPDATE ON engine_vpn_access_profile
            WHEN {condition} BEGIN SELECT RAISE(ABORT, 'VPN profile identity is immutable'); END""")
    else:
        raise RuntimeError("Unsupported VPN persistence backend")


def remove_guard(apps, schema_editor):
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS engine_vpn_identity_immutable"
        + (" ON engine_vpn_access_profile" if schema_editor.connection.vendor == "postgresql" else "")
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS engine_guard_vpn_identity()")


class Migration(migrations.Migration):
    dependencies = [("engine", "0101_vpn_crl_publication")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
