"""Historical-schema proof for the revision-author backfill (#2052)."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

BEFORE = [("ctf", "0064_ctfflag_plaintext_value")]
AFTER = [("ctf", "0065_communication_attribution")]

pytestmark = [pytest.mark.django_db(transaction=True)]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def historical_schema():
    apps = _migrate(BEFORE)
    try:
        yield apps
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_backfill_attributes_only_the_first_revision_to_the_campaign_author(historical_schema):
    author = historical_schema.get_model("auth", "User").objects.create(username="legacy-author")
    campaign = historical_schema.get_model("ctf", "CommunicationCampaign").objects.create(
        workspace_id=1,
        title="Legacy",
        origin="organizer_staff",
        created_by=author,
        actor_token_id=7,
        audience_spec={"kind": "event", "event_ids": []},
        trigger_spec={"kind": "manual"},
        channels=["in_app"],
    )
    revisions = historical_schema.get_model("ctf", "MessageRevision")
    for number in (1, 2):
        revisions.objects.create(
            campaign=campaign, revision_number=number, subject="s", body="b", content_digest="sha256:" + "0" * 64
        )

    apps = _migrate(AFTER)

    migrated = apps.get_model("ctf", "MessageRevision").objects.filter(campaign_id=campaign.pk)
    first, second = migrated.get(revision_number=1), migrated.get(revision_number=2)
    assert (first.actor_user_id, first.actor_token_id) == (author.pk, 7)
    assert (second.actor_user_id, second.actor_token_id) == (None, None)
