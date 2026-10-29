"""Real native asset admissions and storage-boundary fixtures shared by retained contracts."""

from types import SimpleNamespace

import pytest

from tests.cms.services.test_principal_agents import scoped_agents
from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_ingestion import content_administrator
from tests.cms.services.test_principal_uploads import storage_client

__all__ = ["content_actor", "content_administrator", "scoped_agents", "storage_client"]


@pytest.fixture
def installer_upload(content_administrator, storage_client, windows_os):
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    actor = content_administrator
    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    result = initiate_upload(actor.credential, "Native installer", "installer.msi", 1000, scope=actor.scope)
    return SimpleNamespace(actor=actor, client=storage_client, result=result, row=AgentUpload.objects.get())
