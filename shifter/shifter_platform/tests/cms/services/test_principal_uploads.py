"""Uploads retain original scope and proof before issuing storage capabilities."""

from unittest.mock import MagicMock

import pytest

from cms.exceptions import CMSError
from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_ingestion import content_administrator

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]
__all__ = ["content_actor", "content_administrator"]


@pytest.fixture
def storage_client(settings, monkeypatch):
    settings.CLOUD_PROVIDER = "aws"
    settings.AWS_S3_BUCKET_NAME = "synthetic-upload-bucket"
    settings.AGENT_USER_STORAGE_QUOTA_MB = 1
    client = MagicMock()
    client.generate_presigned_url.return_value = "https://storage.example.test/PRIVATE_URL_MARKER"
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)
    return client


def test_service_upload_persists_reference_and_reserves_customer_storage(content_administrator, storage_client):
    from cms.models import AgentUpload
    from cms.services import initiate_upload

    fixture = content_administrator
    result = initiate_upload(fixture.credential, "Synthetic installer", "installer.msi", 600_000, scope=fixture.scope)
    row = AgentUpload.objects.get()
    assert row.initiating_principal_uuid == fixture.credential.principal.uuid
    assert row.credential_uuid == fixture.credential.credential_uuid
    assert row.authority_account_uuid == fixture.scope.account_uuid
    assert row.storage_bucket == "synthetic-upload-bucket"
    assert result["s3_key"] == row.staging_key
    assert row.staging_key != row.install_key
    assert result["upload_token"] not in repr(row.__dict__)
    assert "PRIVATE_URL_MARKER" not in repr(row.__dict__)
    with pytest.raises(CMSError):
        initiate_upload(fixture.credential, "Second installer", "installer.msi", 600_000, scope=fixture.scope)
    assert AgentUpload.objects.count() == 1
    assert storage_client.generate_presigned_url.call_count == 1


def test_foreign_scope_and_read_only_credential_issue_no_upload_url(content_actor, storage_client):
    from cms.services import initiate_upload

    for scope in (content_actor.scope, content_actor.sibling):
        with pytest.raises(CMSError):
            initiate_upload(content_actor.credential, "Synthetic installer", "installer.msi", 100, scope=scope)
    storage_client.generate_presigned_url.assert_not_called()


def test_service_finalization_uses_original_proof_and_immutable_install_key(
    content_administrator, storage_client, windows_os
):
    from cms.models import AgentConfig, AgentUpload
    from cms.services import complete_upload, initiate_upload

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic installer", "installer.msi", 1000, scope=fixture.scope)
    agent = complete_upload(fixture.credential, initiation["upload_token"])
    upload = AgentUpload.objects.get()
    assert agent.owner_principal_uuid == fixture.credential.principal.uuid
    assert agent.account_uuid == fixture.scope.account_uuid
    assert agent.s3_key == upload.install_key
    assert agent.s3_key != initiation["s3_key"]
    assert upload.state == "completed"
    assert storage_client.get_object.call_args.kwargs["IfMatch"] == "synthetic-etag"
    assert storage_client.copy_object.call_args.kwargs["CopySourceIfMatch"] == "synthetic-etag"
    assert complete_upload(fixture.credential, initiation["upload_token"]).pk == agent.pk
    assert AgentConfig.objects.count() == 1


def test_revocation_prevents_finalization_and_provider_read(content_administrator, storage_client):
    from cms.models import AgentConfig
    from cms.services import complete_upload, initiate_upload
    from management.models import ServiceCredentialAdmission

    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic installer", "installer.msi", 1000, scope=fixture.scope)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    with pytest.raises(CMSError):
        complete_upload(fixture.credential, initiation["upload_token"])
    storage_client.head_object.assert_not_called()
    assert not AgentConfig.objects.exists()


def test_revocation_after_storage_copy_prevents_catalog_admission(content_administrator, storage_client, windows_os):
    from cms.models import AgentConfig, AgentUpload
    from cms.services import complete_upload, initiate_upload
    from management.models import ServiceCredentialAdmission

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic installer", "installer.msi", 1000, scope=fixture.scope)

    def copied_then_revoked(**kwargs):
        ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
        return {}

    storage_client.copy_object.side_effect = copied_then_revoked
    with pytest.raises(CMSError):
        complete_upload(fixture.credential, initiation["upload_token"])
    assert not AgentConfig.objects.exists()
    upload = AgentUpload.objects.get()
    assert upload.state == "cancelled"
    assert upload.cleanup_pending
    assert upload.validated_identity["etag"] == "synthetic-etag"


def test_upload_proof_and_storage_identity_cannot_be_rewritten(content_administrator, storage_client):
    from uuid import uuid4

    from django.db import IntegrityError, transaction

    from cms.models import AgentUpload
    from cms.services import initiate_upload

    initiate_upload(
        content_administrator.credential, "Synthetic", "installer.msi", 1000, scope=content_administrator.scope
    )
    upload = AgentUpload.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentUpload.objects.filter(pk=upload.pk).update(credential_uuid=uuid4())


def test_cancel_retains_reservation_until_url_expiry_and_cleanup(content_administrator, storage_client, monkeypatch):
    from datetime import timedelta

    from cms.models import AgentUpload
    from cms.services import cancel_upload, initiate_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic", "installer.msi", 600_000, scope=fixture.scope)
    row = AgentUpload.objects.get()
    cancel_upload(fixture.credential, initiation["upload_token"])
    row.refresh_from_db()
    assert row.state == "cancelled"
    assert reconcile_upload_cleanup() == 0
    storage_client.delete_object.assert_not_called()
    with pytest.raises(CMSError):
        initiate_upload(fixture.credential, "Second", "installer.msi", 600_000, scope=fixture.scope)
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: row.expires_at + timedelta(seconds=1))
    assert reconcile_upload_cleanup() == 1
    row.refresh_from_db()
    assert not row.cleanup_pending
    assert {call.kwargs["Key"] for call in storage_client.delete_object.call_args_list} == {
        row.staging_key,
        row.install_key,
    }


def test_completed_upload_cleanup_preserves_the_installed_object(
    content_administrator, storage_client, windows_os, monkeypatch
):
    from datetime import timedelta

    from cms.models import AgentUpload
    from cms.services import complete_upload, initiate_upload
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic", "installer.msi", 1000, scope=fixture.scope)
    agent = complete_upload(fixture.credential, initiation["upload_token"])
    row = AgentUpload.objects.get()
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: row.expires_at + timedelta(seconds=1))
    assert reconcile_upload_cleanup() == 1
    assert [call.kwargs["Key"] for call in storage_client.delete_object.call_args_list] == [row.staging_key]
    agent.refresh_from_db()
    assert agent.deleted_at is None


def test_service_deletes_completed_installer_with_scope_audit_and_cleanup(
    content_administrator, storage_client, windows_os, monkeypatch
):
    from datetime import timedelta

    from cms.models import AgentUpload
    from cms.services import complete_upload, create_agent, delete_agent, initiate_upload, list_agents
    from cms.services._scoped_uploads import reconcile_upload_cleanup

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic", "installer.msi", 1000, scope=fixture.scope)
    agent = create_agent(fixture.credential, upload_token=initiation["upload_token"])
    upload = AgentUpload.objects.get()
    delete_agent(fixture.credential, agent.pk)
    assert list_agents(fixture.credential, scope=fixture.scope) == []
    with pytest.raises(CMSError):
        complete_upload(fixture.credential, initiation["upload_token"])
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: upload.expires_at + timedelta(seconds=1))
    assert reconcile_upload_cleanup() == 1
    assert upload.install_key in {call.kwargs["Key"] for call in storage_client.delete_object.call_args_list}


@pytest.mark.django_db(transaction=True)
def test_cleanup_preserves_deletion_that_commits_after_its_retirement_snapshot(
    content_administrator, storage_client, windows_os, monkeypatch
):
    import threading
    from datetime import timedelta

    from django.db import close_old_connections, connection

    from cms.models import AgentConfig, AgentUpload
    from cms.services import complete_upload, delete_agent, initiate_upload
    from cms.services._scoped_uploads import _cleanup_upload, reconcile_upload_cleanup

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    actor = content_administrator
    admission = initiate_upload(actor.credential, "Synthetic", "installer.msi", 1000, scope=actor.scope)
    agent = complete_upload(actor.credential, admission["upload_token"])
    upload = AgentUpload.objects.get()
    inspected, resume = threading.Event(), threading.Event()
    failures = []

    def cleanup():
        close_old_connections()
        suspended = False

        def after_retirement_query(execute, sql, params, many, context):
            nonlocal suspended
            result = execute(sql, params, many, context)
            if not suspended and AgentConfig._meta.db_table in sql and "IS NOT NULL" in sql:
                suspended = True
                inspected.set()
                assert resume.wait(10)
            return result

        try:
            with connection.execute_wrapper(after_retirement_query):
                _cleanup_upload(upload)
        except CMSError:
            pass
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            connection.close()

    worker = threading.Thread(target=cleanup)
    worker.start()
    try:
        assert inspected.wait(10)
        delete_agent(actor.credential, agent.pk)
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    upload.refresh_from_db()
    assert upload.cleanup_pending
    monkeypatch.setattr("cms.services._scoped_uploads.timezone.now", lambda: upload.expires_at + timedelta(seconds=1))
    assert reconcile_upload_cleanup() == 1
    assert upload.install_key in {call.kwargs["Key"] for call in storage_client.delete_object.call_args_list}


def test_source_agent_creation_cannot_invent_another_storage_key(content_administrator):
    from cms.models import AgentConfig
    from cms.services import create_agent

    with pytest.raises(CMSError):
        create_agent(
            content_administrator.credential,
            name="Synthetic",
            s3_key="foreign/customer/object",
            filename="installer.msi",
            os_slug="windows",
            file_size=1000,
        )
    assert not AgentConfig.objects.exists()


def test_management_only_ceiling_can_delete_an_authorized_installer(content_administrator, storage_client, windows_os):
    from uuid import uuid4

    from cms.services import complete_upload, delete_agent, initiate_upload
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential

    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    fixture = content_administrator
    initiation = initiate_upload(fixture.credential, "Synthetic", "installer.msi", 1000, scope=fixture.scope)
    agent = complete_upload(fixture.credential, initiation["upload_token"])
    old = ServiceCredentialAdmission.objects.select_related("binding").get(uuid=fixture.credential.credential_uuid)
    binding = ProviderBinding.objects.create(
        principal=old.binding.principal, issuer=old.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=old.audience,
        scopes=["authorization:account.manage_content"],
    )
    credential = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    delete_agent(credential, agent.pk)
    agent.refresh_from_db()
    assert agent.deleted_at is not None
