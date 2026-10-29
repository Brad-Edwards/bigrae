"""Upgrades never invent a versioned effect plan for unfinished legacy authority."""

from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

BEFORE = [("ctf", "0075_range_launch_intent_guards")]
AFTER = [("ctf", "0076_event_authority_effect_plan")]
pytestmark = pytest.mark.django_db(transaction=True)


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture
def legacy_authority_schema():
    apps = migrate(BEFORE)
    try:
        yield apps
    finally:
        # Test-owned historical rows only; the production migration changes none.
        apps.get_model("ctf", "CTFEventAuthorityTransition").objects.filter(state="pending").update(state="confirmed")
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.parametrize("state", ["pending", "confirmed"])
def test_authority_upgrade_blocks_pending_without_rewriting_history(legacy_authority_schema, state):
    from django.contrib.auth import get_user_model

    from workspaces.models import Account

    user = get_user_model().objects.create_user(username="historical-owner")
    account = Account.objects.create(kind="team", name="Historical scope")
    event = legacy_authority_schema.get_model("ctf", "CTFEvent").objects.create(
        name="Historical transition",
        created_by_id=user.pk,
        creator_principal_uuid=user.identity_principal.uuid,
        owner_principal_uuid=user.identity_principal.uuid,
        scope_kind="account",
        account_id=account.pk,
        scenario_id="synthetic",
        event_start=timezone.now(),
        event_end=timezone.now(),
    )
    transition = legacy_authority_schema.get_model("ctf", "CTFEventAuthorityTransition").objects.create(
        event=event,
        kind="transfer",
        state=state,
        actor_principal_uuid=user.identity_principal.uuid,
        actor_credential_uuid=uuid4(),
        target_principal_uuid=uuid4(),
        previous_owner_principal_uuid=user.identity_principal.uuid,
    )
    original = (
        transition.actor_credential_uuid,
        transition.target_principal_uuid,
        transition.previous_owner_principal_uuid,
    )
    if state == "pending":
        with pytest.raises(RuntimeError, match="settled on the previous version"):
            migrate(AFTER)
        with connection.cursor() as cursor:
            fields = connection.introspection.get_table_description(cursor, "ctf_event_authority_transition")
        assert "effect_plan" not in {field.name for field in fields}
        retained = type(transition).objects.get(pk=transition.pk)
    else:
        retained = migrate(AFTER).get_model("ctf", "CTFEventAuthorityTransition").objects.get(pk=transition.pk)
        assert retained.effect_plan == {} and retained.effect_plan_digest == ""
    assert retained.state == state
    assert (
        retained.actor_credential_uuid,
        retained.target_principal_uuid,
        retained.previous_owner_principal_uuid,
    ) == original
