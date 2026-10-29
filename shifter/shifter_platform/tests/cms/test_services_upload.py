"""Native upload initiation validates input and reserves bytes before signing capabilities."""

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


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", None),
        ("name", " "),
        ("name", "x" * 101),
        ("filename", None),
        ("filename", ""),
        ("filename", "x" * 256),
        ("filename", "installer.exe"),
        ("file_size", None),
        ("file_size", True),
        ("file_size", "1000"),
        ("file_size", 0),
        ("file_size", -1),
        ("agent_type", "unknown"),
    ],
)
def test_native_upload_rejects_invalid_metadata_before_storage(content_administrator, storage_client, field, value):
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    values = {"name": "Native installer", "filename": "installer.msi", "file_size": 1000, "agent_type": "xdr"}
    values[field] = value
    with pytest.raises(CMSError):
        initiate_upload(content_administrator.credential, scope=content_administrator.scope, **values)
    assert not AgentUpload.objects.exists()
    storage_client.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize(
    "filename,os_slug",
    [
        ("installer.msi", "windows"),
        ("installer.deb", "linux-debian"),
        ("installer.rpm", "linux-rhel"),
        ("installer.tar.gz", "linux-generic"),
    ],
)
def test_native_upload_token_and_signed_request_match_persisted_admission(
    content_administrator, storage_client, filename, os_slug
):
    from cms.assets.upload_token import verify_upload_token
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    actor = content_administrator
    result = initiate_upload(
        actor.credential, "Native installer", filename, 1000, agent_type="xdr_collector", scope=actor.scope
    )
    row = AgentUpload.objects.get()
    payload = verify_upload_token(result["upload_token"], actor.credential.principal.uuid)
    assert payload["upload_id"] == str(row.pk)
    assert payload["s3_key"] == row.staging_key == result["s3_key"]
    assert payload["agent_type"] == "xdr_collector" and result["expected_os"] == os_slug
    assert payload["file_size"] == 1000
    assert storage_client.generate_presigned_url.call_args.kwargs["Params"]["ContentLength"] == 1000


def test_native_file_limit_and_scope_quota_accept_exact_bound_and_reject_excess(
    content_administrator, storage_client, settings
):
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    settings.AGENT_MAX_FILE_SIZE_MB = settings.AGENT_USER_STORAGE_QUOTA_MB = 1
    actor = content_administrator
    with pytest.raises(CMSError):
        initiate_upload(actor.credential, "Oversized", "installer.msi", 1024 * 1024 + 1, scope=actor.scope)
    assert not AgentUpload.objects.exists()
    storage_client.generate_presigned_url.assert_not_called()
    initiate_upload(actor.credential, "Exact bound", "installer.msi", 1024 * 1024, scope=actor.scope)
    with pytest.raises(CMSError, match="quota"):
        initiate_upload(actor.credential, "Over quota", "installer.msi", 1, scope=actor.scope)
    assert AgentUpload.objects.count() == 1


def test_uncertain_presign_keeps_cancelled_reservation_and_cleanup(content_administrator, storage_client):
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    storage_client.generate_presigned_url.side_effect = ConnectionError("Synthetic timeout")
    with pytest.raises(CMSError):
        initiate_upload(
            content_administrator.credential, "Uncertain", "installer.msi", 1000, scope=content_administrator.scope
        )
    row = AgentUpload.objects.get()
    assert row.state == "cancelled" and row.cleanup_pending
