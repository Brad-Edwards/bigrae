"""Current native admission must survive until each owning CMS transaction commits."""

from datetime import timedelta

import pytest

from cms.exceptions import CMSError
from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_ingestion import content_administrator
from tests.cms.services.test_principal_uploads import storage_client
from tests.cms.test_runtime_plugins import tenant

__all__ = ["content_actor", "content_administrator", "storage_client", "tenant"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        ("create_credential", ("deployment_profile",), {"name": "Unproven", "authcode": "synthetic"}),
        ("delete_credential", (1,), {}),
        ("get_credential", (1,), {}),
        ("list_credentials", (), {}),
        ("create_agent", (), {"name": "Unproven", "s3_key": "synthetic/installer"}),
        ("delete_agent", (1,), {}),
        ("get_agent", (1,), {}),
        ("list_agents", (), {}),
        ("initiate_upload", ("Unproven", "installer.msi", 1000), {}),
        ("complete_upload", ("synthetic-token",), {}),
        ("cancel_upload", ("synthetic-token",), {}),
        ("get_storage_used", (), {}),
        ("create_ngfw", ("Unproven", 1, "otp"), {}),
        ("destroy_ngfw", ("00000000-0000-0000-0000-000000000001", "Unproven"), {}),
        ("get_ngfw", ("00000000-0000-0000-0000-000000000001",), {}),
        ("list_ngfws", (), {}),
    ],
)
def test_raw_user_and_flags_never_substitute_for_native_asset_proof(
    django_user_model, monkeypatch, operation, args, kwargs
):
    from unittest.mock import Mock

    from cms import services
    from cms.models import AgentConfig, AgentUpload, Credential, Request
    from engine.models import ProvisionerLaunchIntent

    user = django_user_model.objects.create_superuser(username="unproven-asset-actor", password="unused")
    storage = Mock(side_effect=AssertionError("An unproven caller must not issue a storage capability"))
    monkeypatch.setattr("cms.services._scoped_uploads.get_object_storage", storage)
    with pytest.raises(CMSError, match="Content unavailable"):
        getattr(services, operation)(user, *args, **kwargs)
    assert not Credential.objects.exists()
    assert not AgentConfig.objects.exists() and not AgentUpload.objects.exists()
    assert not Request.objects.exists() and not ProvisionerLaunchIntent.objects.exists()
    storage.assert_not_called()


def test_public_asset_facade_does_not_export_the_retired_raw_mutators():
    from cms import services

    assert not hasattr(services, "assets_create_agent")
    assert not hasattr(services, "assets_delete_agent")


def _revoke(credential):
    from management.models import ServiceCredentialAdmission

    ServiceCredentialAdmission.objects.filter(uuid=credential.credential_uuid).update(is_active=False)


@pytest.mark.django_db(transaction=True)
def test_human_credential_admission_is_outside_sql_and_revocation_fences_real_commit(tenant, monkeypatch):
    from django.db import connection

    from cms.models import Credential, CredentialType
    from cms.services import _scoped_credentials, create_credential
    from management.models import PrincipalSession
    from workspaces.services import hierarchy_target_scope

    actor, organization = tenant
    CredentialType.objects.get_or_create(
        slug="deployment_profile", defaults={"name": "Deployment profile", "spec_slug": "credential.deployment_profile"}
    )
    original = _scoped_credentials.require_content_scope
    calls = 0

    def admitted_then_revoke(*args, **kwargs):
        nonlocal calls
        assert not connection.in_atomic_block
        current = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            PrincipalSession.objects.filter(credential_uuid=current.credential_uuid).delete()
        return current

    monkeypatch.setattr(_scoped_credentials, "require_content_scope", admitted_then_revoke)
    with pytest.raises(CMSError):
        create_credential(
            actor.credential,
            "deployment_profile",
            scope=hierarchy_target_scope("organization", organization.uuid),
            name="Revoked human",
            authcode="synthetic",
        )
    assert not Credential.objects.exists()


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_private_credential_commit_rejects_revocation_after_final_admission(content_actor, monkeypatch, operation):
    from cms.models import Credential
    from cms.services import _scoped_credentials, create_credential, delete_credential

    actor = content_actor
    created = None
    if operation == "delete":
        created = create_credential(
            actor.credential, "deployment_profile", scope=actor.scope, name="Retained source", authcode="synthetic"
        )
    original = _scoped_credentials.require_content_scope
    calls = 0

    def admit_then_revoke(*args, **kwargs):
        nonlocal calls
        current = original(*args, **kwargs)
        calls += 1
        if calls == (2 if operation == "create" else 1):
            _revoke(current)
        return current

    monkeypatch.setattr(_scoped_credentials, "require_content_scope", admit_then_revoke)
    with pytest.raises(CMSError):
        if operation == "create":
            create_credential(
                actor.credential, "deployment_profile", scope=actor.scope, name="Late revoke", authcode="synthetic"
            )
        else:
            delete_credential(actor.credential, created.credential_id)
    assert not Credential.objects.filter(name="Late revoke").exists()
    if created:
        assert Credential.objects.filter(pk=created.credential_id).exists()


def test_upload_reservation_rechecks_proof_before_issuing_provider_capability(
    content_administrator, storage_client, monkeypatch
):
    from cms.models import AgentUpload
    from cms.services import _scoped_uploads, initiate_upload

    original = _scoped_uploads._upload_values

    def values_then_revoke(*args, **kwargs):
        values = original(*args, **kwargs)
        _revoke(content_administrator.credential)
        return values

    monkeypatch.setattr(_scoped_uploads, "_upload_values", values_then_revoke)
    with pytest.raises(CMSError):
        initiate_upload(
            content_administrator.credential, "Revoked upload", "installer.msi", 1000, scope=content_administrator.scope
        )
    assert not AgentUpload.objects.exists()
    storage_client.generate_presigned_url.assert_not_called()


@pytest.mark.parametrize("phase", ["finalizing", "installer", "expiry", "original_proof"])
def test_upload_each_commit_fences_current_and_original_proof_and_expiry(
    content_administrator, storage_client, windows_os, monkeypatch, phase
):
    from uuid import uuid4

    from cms.models import AgentConfig, AgentUpload
    from cms.services import _scoped_uploads, complete_upload, initiate_upload
    from management.models import ServiceCredentialAdmission
    from shared.credentials import CredentialContext
    from tests._credential_helpers import persisted_credential

    actor = content_administrator
    initiation = initiate_upload(actor.credential, "Synthetic installer", "installer.msi", 1000, scope=actor.scope)
    original_proof = actor.credential
    if phase == "original_proof":
        actor.credential = persisted_credential(
            CredentialContext(
                original_proof.principal, "service", uuid4(), original_proof.ceiling, original_proof.scopes
            )
        )
    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    original = _scoped_uploads._authorized_upload
    calls = 0

    def authorized_then_expire_or_revoke(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == (1 if phase == "finalizing" else 3):
            if phase == "expiry":
                expiry = AgentUpload.objects.get().expires_at
                monkeypatch.setattr(_scoped_uploads.timezone, "now", lambda: expiry + timedelta(seconds=1))
            else:
                _revoke(original_proof)
        return result

    monkeypatch.setattr(_scoped_uploads, "_authorized_upload", authorized_then_expire_or_revoke)
    with pytest.raises(CMSError):
        complete_upload(actor.credential, initiation["upload_token"])
    assert not AgentConfig.objects.exists()
    row = AgentUpload.objects.get()
    assert row.cleanup_pending
    assert row.state == ("issued" if phase == "finalizing" else "cancelled")
    if phase == "finalizing":
        storage_client.head_object.assert_not_called()
    if phase == "original_proof":
        assert ServiceCredentialAdmission.objects.get(uuid=actor.credential.credential_uuid).is_active


def test_upload_cancel_cannot_commit_after_its_admitted_proof_is_revoked(
    content_administrator, storage_client, monkeypatch
):
    from cms.models import AgentUpload
    from cms.services import _scoped_uploads, cancel_upload, initiate_upload

    actor = content_administrator
    initiation = initiate_upload(actor.credential, "Synthetic installer", "installer.msi", 1000, scope=actor.scope)
    original = _scoped_uploads.require_content_scope

    def admitted_then_revoke(*args, **kwargs):
        current = original(*args, **kwargs)
        _revoke(current)
        return current

    monkeypatch.setattr(_scoped_uploads, "require_content_scope", admitted_then_revoke)
    with pytest.raises(CMSError):
        cancel_upload(actor.credential, initiation["upload_token"])
    assert AgentUpload.objects.get().state == "issued"


def test_installer_retirement_cannot_commit_after_its_final_admission_is_revoked(
    content_administrator, storage_client, windows_os, monkeypatch
):
    from cms.models import AgentConfig, AgentUpload
    from cms.services import _scoped_agents, complete_upload, delete_agent, initiate_upload

    actor = content_administrator
    storage_client.head_object.return_value = {"ContentLength": 1000, "ETag": '"synthetic-etag"'}
    storage_client.get_object.return_value["Body"].read.return_value = bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 16
    initiation = initiate_upload(actor.credential, "Synthetic installer", "installer.msi", 1000, scope=actor.scope)
    agent = complete_upload(actor.credential, initiation["upload_token"])
    AgentUpload.objects.update(cleanup_pending=False)
    original = _scoped_agents.require_content_scope
    calls = 0

    def admitted_then_revoke(*args, **kwargs):
        nonlocal calls
        current = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            _revoke(current)
        return current

    monkeypatch.setattr(_scoped_agents, "require_content_scope", admitted_then_revoke)
    with pytest.raises(CMSError):
        delete_agent(actor.credential, agent.pk)
    assert AgentConfig.objects.filter(pk=agent.pk).exists()
    assert not AgentUpload.objects.get().cleanup_pending
