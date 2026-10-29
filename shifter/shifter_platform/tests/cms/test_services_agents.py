"""Installer services require native proof, scoped custody and a verified upload."""

import pytest

from cms.exceptions import CMSError
from tests.cms._native_asset_fixtures import (
    content_actor,
    content_administrator,
    installer_upload,
    scoped_agents,
    storage_client,
)

__all__ = ["content_actor", "content_administrator", "installer_upload", "scoped_agents", "storage_client"]
pytestmark = pytest.mark.django_db


def test_native_agent_projection_matches_the_public_contract(content_actor, scoped_agents):
    from cms.services import get_agent, list_agents
    from shared.schemas.cms_projections import AgentListItem

    rows = list_agents(content_actor.credential, scope=content_actor.scope)
    assert len(rows) == 1 and rows[0]["id"] == scoped_agents[0].pk
    assert set(rows[0]) == set(AgentListItem.__annotations__)
    assert get_agent(content_actor.credential, rows[0]["id"]).pk == rows[0]["id"]
    assert "synthetic/customer" not in repr(rows)
    with pytest.raises(CMSError):
        get_agent(content_actor.credential, scoped_agents[1].pk)


@pytest.mark.parametrize(
    "values",
    [{}, {"s3_key": "synthetic/claimed", "name": "Unverified"}, {"upload_token": "token", "name": "Unverified"}],
)
def test_native_agent_cannot_be_created_from_asserted_storage_metadata(content_administrator, values):
    from cms.models import AgentConfig
    from cms.services import create_agent

    with pytest.raises(CMSError, match="verified upload"):
        create_agent(content_administrator.credential, **values)
    assert not AgentConfig.objects.exists()


def test_native_create_and_retire_keep_exact_cleanup_custody(installer_upload):
    from cms.models import AgentUpload
    from cms.services import create_agent, delete_agent, get_agent, list_agents

    case = installer_upload
    agent = create_agent(case.actor.credential, upload_token=case.result["upload_token"])
    assert agent.user_id is None and agent.owner_principal_uuid == case.actor.credential.principal.uuid
    assert agent.s3_key == case.row.install_key
    assert get_agent(case.actor.credential, agent.pk).pk == agent.pk
    delete_agent(case.actor.credential, agent.pk)
    assert not list_agents(case.actor.credential, scope=case.actor.scope)
    with pytest.raises(CMSError):
        get_agent(case.actor.credential, agent.pk)
    assert AgentUpload.objects.get().cleanup_pending
    case.client.delete_object.assert_not_called()
