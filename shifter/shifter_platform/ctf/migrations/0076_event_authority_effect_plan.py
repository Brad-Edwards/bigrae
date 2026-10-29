"""Retain exact versioned authority effects; never infer unfinished legacy intent."""

from django.db import migrations, models


def require_settled_legacy_transitions(apps, schema_editor):
    transition = apps.get_model("ctf", "CTFEventAuthorityTransition")
    if transition.objects.using(schema_editor.connection.alias).filter(state="pending").exists():
        raise RuntimeError(
            "Unfinished legacy event authority transitions must be settled on the previous version "
            "before upgrade; no rows were changed."
        )


def install_guard(apps, schema_editor):
    fields = (
        "id",
        "event_id",
        "kind",
        "actor_principal_uuid",
        "actor_credential_uuid",
        "target_principal_uuid",
        "previous_owner_principal_uuid",
        "previous_role",
        "requested_role",
        "effect_plan",
        "effect_plan_digest",
    )
    table = "ctf_event_authority_transition"
    if schema_editor.connection.vendor == "postgresql":
        changed = " OR ".join(f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in fields)
        schema_editor.execute(f"""CREATE FUNCTION ctf_guard_event_authority_plan() RETURNS trigger AS $$
            BEGIN IF {changed} OR (OLD.state = 'confirmed' AND NEW.state <> OLD.state) THEN
                RAISE EXCEPTION 'event authority intent is immutable'; END IF; RETURN NEW; END;
            $$ LANGUAGE plpgsql""")
        schema_editor.execute(f"""CREATE TRIGGER ctf_event_authority_plan_immutable BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION ctf_guard_event_authority_plan()""")
    elif schema_editor.connection.vendor == "sqlite":
        changed = " OR ".join(f"NEW.{field} IS NOT OLD.{field}" for field in fields)
        schema_editor.execute(f"""CREATE TRIGGER ctf_event_authority_plan_immutable BEFORE UPDATE ON {table}
            WHEN {changed} OR (OLD.state = 'confirmed' AND NEW.state <> OLD.state)
            BEGIN SELECT RAISE(ABORT, 'event authority intent is immutable'); END""")
    else:
        raise RuntimeError("Unsupported event authority persistence backend")


def remove_guard(apps, schema_editor):
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS ctf_event_authority_plan_immutable"
        + (" ON ctf_event_authority_transition" if schema_editor.connection.vendor == "postgresql" else "")
    )
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS ctf_guard_event_authority_plan()")


class Migration(migrations.Migration):
    dependencies = [("ctf", "0075_range_launch_intent_guards")]
    operations = [
        migrations.RunPython(require_settled_legacy_transitions, migrations.RunPython.noop),
        migrations.AddField(
            model_name="ctfeventauthoritytransition",
            name="effect_plan",
            field=models.JSONField(default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="ctfeventauthoritytransition",
            name="effect_plan_digest",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="ctfeventauthoritytransition",
            constraint=models.CheckConstraint(
                condition=~models.Q(state="pending") | ~models.Q(effect_plan_digest=""),
                name="ctf_pending_authority_has_effect_plan",
            ),
        ),
        migrations.RunPython(install_guard, remove_guard),
    ]
