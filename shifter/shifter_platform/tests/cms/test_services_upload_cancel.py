"""Native cancellation retains a usable signed capability's cleanup reservation."""

from datetime import timedelta

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


def test_native_cancellation_is_idempotent_and_defers_delete_until_url_expiry(installer_upload, monkeypatch):
    from cms.models import AgentUpload
    from cms.services import cancel_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    case = installer_upload
    cancel_upload(case.actor.credential, case.result["upload_token"])
    cancel_upload(case.actor.credential, case.result["upload_token"])
    row = AgentUpload.objects.get()
    assert row.state == "cancelled" and row.cleanup_pending
    case.client.delete_object.assert_not_called()
    assert reconcile_upload_cleanup() == 0
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: row.expires_at + timedelta(seconds=1))
    assert reconcile_upload_cleanup() == 1
    assert {call.kwargs["Key"] for call in case.client.delete_object.call_args_list} == {
        row.staging_key,
        row.install_key,
    }


@pytest.mark.parametrize("token", [None, "", " ", "not-a-token", "x" * 17000])
def test_native_cancel_refuses_untrusted_tokens_without_storage(content_administrator, storage_client, token):
    from cms.services import cancel_upload

    with pytest.raises(CMSError):
        cancel_upload(content_administrator.credential, token)
    storage_client.delete_object.assert_not_called()


def test_failed_native_cleanup_stays_durable_and_retries(installer_upload, monkeypatch):
    from cms.models import AgentUpload
    from cms.services import cancel_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    case = installer_upload
    cancel_upload(case.actor.credential, case.result["upload_token"])
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: case.row.expires_at + timedelta(seconds=1))
    case.client.delete_object.side_effect = ConnectionError("Synthetic delete timeout")
    assert reconcile_upload_cleanup() == 0
    assert AgentUpload.objects.get().cleanup_pending
    case.client.delete_object.side_effect = None
    assert reconcile_upload_cleanup() == 1
    assert not AgentUpload.objects.get().cleanup_pending
