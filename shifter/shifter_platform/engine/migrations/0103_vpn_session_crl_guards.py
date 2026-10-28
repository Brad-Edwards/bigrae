"""Protect session custody and monotonic public CRL publication across bulk writes."""

from django.db import migrations


def install_guard(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    comparison = "IS DISTINCT FROM" if vendor == "postgresql" else "IS NOT"
    immutable = ("id", "range_id", "profile_id", "generation", "server", "client_id", "started_at")
    session_condition = " OR ".join(f"NEW.{name} {comparison} OLD.{name}" for name in immutable)
    session_condition += " OR (OLD.state = 'ended' AND NEW.state <> 'ended')"
    session_condition += f" OR (OLD.ended_at IS NOT NULL AND NEW.ended_at {comparison} OLD.ended_at)"
    crl_condition = "NEW.id <> OLD.id OR NEW.requested_revision < OLD.requested_revision"
    crl_condition += " OR NEW.published_revision < OLD.published_revision"
    crl_condition += " OR NEW.published_revision > NEW.requested_revision"
    crl_condition += (
        f" OR (OLD.issuer_fingerprint <> '' AND NEW.issuer_fingerprint {comparison} OLD.issuer_fingerprint)"
    )
    changed = " OR ".join(f"NEW.{name} {comparison} OLD.{name}" for name in ("issuer_certificate", "pem", "expires_at"))
    crl_condition += (
        f" OR (OLD.published_revision > 0 AND NEW.published_revision = OLD.published_revision AND ({changed}))"
    )
    for name, table, condition in (
        ("vpn_session", "engine_vpn_session", session_condition),
        ("vpn_crl", "engine_vpn_crl_state", crl_condition),
    ):
        if vendor == "postgresql":
            schema_editor.execute(f"""CREATE FUNCTION engine_guard_{name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF {condition} THEN
                    RAISE EXCEPTION 'VPN custody or publication is immutable' USING ERRCODE = '23514';
                END IF; RETURN NEW; END; $$""")
            schema_editor.execute(f"""CREATE TRIGGER engine_{name}_immutable BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION engine_guard_{name}()""")
        elif vendor == "sqlite":
            schema_editor.execute(f"""CREATE TRIGGER engine_{name}_immutable BEFORE UPDATE ON {table}
                WHEN {condition} BEGIN SELECT RAISE(ABORT, 'VPN custody or publication is immutable'); END""")
        else:
            raise RuntimeError("Unsupported VPN persistence backend")


def remove_guard(apps, schema_editor):
    for name, table in (("vpn_session", "engine_vpn_session"), ("vpn_crl", "engine_vpn_crl_state")):
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS engine_{name}_immutable"
            + (f" ON {table}" if schema_editor.connection.vendor == "postgresql" else "")
        )
        if schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(f"DROP FUNCTION IF EXISTS engine_guard_{name}()")


class Migration(migrations.Migration):
    dependencies = [("engine", "0102_vpn_identity_guards")]
    operations = [migrations.RunPython(install_guard, remove_guard)]
