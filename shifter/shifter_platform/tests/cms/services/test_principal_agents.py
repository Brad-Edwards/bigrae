"""Installer reads and storage accounting stay within the authorized customer."""

import pytest

from cms.exceptions import CMSError
from tests.cms.services.test_principal_content import content_actor

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]
__all__ = ["content_actor"]


@pytest.fixture
def scoped_agents(content_actor, windows_os):
    from cms.models import AgentConfig

    return [
        AgentConfig.objects.create(
            user=None,
            owner_principal_uuid=content_actor.credential.principal.uuid,
            scope_kind=scope.kind,
            account_uuid=scope.account_uuid,
            name=f"Synthetic installer {index}",
            os=windows_os,
            s3_key=f"synthetic/customer-{index}/installer.msi",
            original_filename="installer.msi",
            file_size_bytes=(index + 1) * 100,
        )
        for index, scope in enumerate((content_actor.scope, content_actor.sibling))
    ]


def test_service_reads_installers_and_quota_only_in_authorized_scope(content_actor, scoped_agents):
    from cms.services import get_agent, get_storage_used, list_agents

    fixture = content_actor
    entries = list_agents(fixture.credential, scope=fixture.scope)
    assert [entry["id"] for entry in entries] == [scoped_agents[0].pk]
    assert "customer-" not in repr(entries)
    assert get_agent(fixture.credential, scoped_agents[0].pk).pk == scoped_agents[0].pk
    assert get_storage_used(fixture.credential, scope=fixture.scope) == 100
    with pytest.raises(CMSError):
        get_agent(fixture.credential, scoped_agents[1].pk)
    with pytest.raises(CMSError):
        list_agents(fixture.credential, scope=fixture.sibling)


def test_revoked_service_cannot_read_installers_or_storage_usage(content_actor, scoped_agents):
    from cms.services import get_agent, get_storage_used
    from management.models import ServiceCredentialAdmission

    ServiceCredentialAdmission.objects.filter(uuid=content_actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises(CMSError):
        get_agent(content_actor.credential, scoped_agents[0].pk)
    with pytest.raises(CMSError):
        get_storage_used(content_actor.credential, scope=content_actor.scope)


def test_historical_user_attribution_does_not_open_an_unscoped_read(scoped_agents, django_user_model):
    from cms.models import AgentConfig
    from cms.services import get_agent, list_agents

    user = django_user_model.objects.create_user(username="synthetic-historical-uploader")
    AgentConfig.objects.filter(pk=scoped_agents[1].pk).update(user=user)
    assert list_agents(user) == []
    with pytest.raises(CMSError):
        get_agent(user, scoped_agents[1].pk)
