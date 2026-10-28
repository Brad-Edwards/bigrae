"""Native completion binds validation, immutable bytes, proof and durable cleanup."""

import pytest
from botocore.exceptions import ClientError

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


def test_native_completion_installs_and_tags_only_the_conditionally_validated_object(installer_upload):
    from cms.models import AgentUpload
    from cms.services import complete_upload
    from shared.models import AuditLog

    case = installer_upload
    agent = complete_upload(case.actor.credential, case.result["upload_token"])
    row = AgentUpload.objects.get()
    assert agent.s3_key == row.install_key and agent.s3_key != row.staging_key
    assert agent.file_size_bytes == 1000
    assert case.client.get_object.call_args.kwargs["IfMatch"] == "synthetic-etag"
    copy = case.client.copy_object.call_args.kwargs
    assert copy["CopySourceIfMatch"] == "synthetic-etag"
    assert copy["CopySource"]["Key"] == row.staging_key and copy["Key"] == row.install_key
    assert case.client.put_object_tagging.call_args.kwargs["Key"] == row.install_key
    assert AuditLog.objects.filter(
        entity_id=agent.pk, action="create", actor_principal_uuid=case.actor.credential.principal.uuid
    ).exists()
    assert row.cleanup_pending
    case.client.delete_object.assert_not_called()


@pytest.mark.parametrize("failure", ["head", "size", "header", "header-version", "copy-version", "lowered-cap"])
def test_native_completion_failure_never_installs_unverified_bytes_and_retains_cleanup(
    installer_upload, settings, failure
):
    from cms.models import AgentConfig, AgentUpload
    from cms.services import complete_upload

    case = installer_upload
    error = ClientError(
        {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
        "SyntheticConditionalRead",
    )
    if failure == "head":
        case.client.head_object.side_effect = error
    elif failure == "size":
        case.client.head_object.return_value["ContentLength"] = 999
    elif failure == "header":
        case.client.get_object.return_value["Body"].read.return_value = b"PK\x03\x04SYNTHETIC_PRIVATE_HEADER"
    elif failure == "header-version":
        case.client.get_object.side_effect = error
    elif failure == "copy-version":
        case.client.copy_object.side_effect = error
    else:
        settings.AGENT_MAX_FILE_SIZE_MB = 0
    with pytest.raises(CMSError):
        complete_upload(case.actor.credential, case.result["upload_token"])
    assert not AgentConfig.objects.exists()
    row = AgentUpload.objects.get()
    assert row.state == "cancelled" and row.cleanup_pending
    case.client.put_object_tagging.assert_not_called()
    if failure != "copy-version":
        case.client.copy_object.assert_not_called()


@pytest.mark.parametrize("token", [None, "", " ", "not-a-token", "x" * 17000])
def test_native_completion_rejects_untrusted_tokens_before_storage(content_administrator, storage_client, token):
    from cms.services import complete_upload

    with pytest.raises(CMSError):
        complete_upload(content_administrator.credential, token)
    storage_client.head_object.assert_not_called()
