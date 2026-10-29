"""Tenant pack bytes and deletion retain the initiating native content authority."""

import pytest
from django.db import connection

from tests.cms.test_tenant_pack_upload import _post, tenant, upload

__all__ = ["tenant", "upload"]
pytestmark = pytest.mark.django_db


def _native_actor(tenant, settings, *, service):
    if not service:
        return tenant[0].credential
    from tests.cms._warm_pool_fixtures import service_credential

    credential = service_credential(
        settings, scopes=["authorization:organization.manage_content", "authorization:organization.read_content"]
    )
    tenant[0].policy_principals.add(credential.principal.uuid)
    return credential


@pytest.mark.parametrize("service", [False, True])
def test_native_actor_installs_reads_and_removes_own_tenant_pack(upload, tenant, settings, service):
    from cms.models import RaesPackageSource, TenantPackRemoval

    client, organization, root, storage = upload
    credential = _native_actor(tenant, settings, service=service)
    client.force_authenticate(user=None if service else tenant[0], token=credential)
    response = _post(client, organization, root)
    assert response.status_code == 201, response.data
    source = RaesPackageSource.objects.get()
    assert source.owner_principal_uuid == credential.principal.uuid
    assert source.scope_kind == "account"
    assert source.account_uuid == organization.account.uuid
    assert source.organization_uuid == organization.uuid
    assert source.workspace_uuid is None
    response = client.get(
        "/api/v1/cms/catalog/",
        {"scope_kind": "account", "account": organization.account.uuid, "organization": organization.uuid},
    )
    assert response.status_code == 200, response.data
    assert [row["id"] for row in response.data] == [source.scenario_id]
    response = client.post(
        f"/api/v1/cms/organizations/{organization.uuid}/packs/{source.scenario_id}/removal/",
        {"pack_digest": source.package_digest},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert not RaesPackageSource.objects.exists() and not storage.objects
    removal = TenantPackRemoval.objects.get()
    assert removal.requested_by_id is None
    assert removal.requested_by_principal_uuid == credential.principal.uuid


@pytest.mark.parametrize("service", [False, True])
def test_sql_membership_or_staff_flags_do_not_substitute_for_tenant_content_policy(upload, tenant, settings, service):
    from cms.models import RaesPackageSource
    from workspaces.models import OrganizationMembership

    client, organization, root, storage = upload
    credential = _native_actor(tenant, settings, service=service)
    client.force_authenticate(user=None if service else tenant[0], token=credential)
    OrganizationMembership.objects.create(user=tenant[0], organization=organization, role="admin")
    tenant[0].is_staff = tenant[0].is_superuser = True
    tenant[0].save(update_fields=["is_staff", "is_superuser"])
    tenant[0].policy_principals.clear()
    response = _post(client, organization, root)
    assert response.status_code == 403, response.data
    assert not RaesPackageSource.objects.exists() and not storage.objects


@pytest.mark.parametrize("service", [False, True])
def test_revoked_tenant_pack_proof_after_storage_cannot_commit(upload, tenant, settings, service, monkeypatch):
    from cms.models import RaesPackageSource, TenantPackArchive
    from management.models import Principal

    client, organization, root, storage = upload
    credential = _native_actor(tenant, settings, service=service)
    client.force_authenticate(user=None if service else tenant[0], token=credential)
    original = storage.upload_file

    def store_then_revoke(*args, **kwargs):
        original(*args, **kwargs)
        Principal.objects.filter(uuid=credential.principal.uuid).update(is_active=False)

    monkeypatch.setattr(storage, "upload_file", store_then_revoke)
    assert _post(client, organization, root).status_code == 400
    assert not RaesPackageSource.objects.exists()
    assert not TenantPackArchive.objects.exists()
    assert not storage.objects


@pytest.mark.django_db(transaction=True)
def test_tenant_upload_and_removal_keep_policy_outside_sql_transactions(upload, tenant, monkeypatch):
    from shared.authorization import configured_authorization_provider

    client, organization, root, _storage = upload
    provider = configured_authorization_provider()
    original = provider.check
    calls = []

    def check(request):
        assert not connection.in_atomic_block, request.action
        calls.append(request.action)
        return original(request)

    monkeypatch.setattr(provider, "check", check)
    # The fixture's provider factory returns a fresh namespace each time.
    from shared.authorization import port

    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    response = _post(client, organization, root)
    assert response.status_code == 201, response.data
    response = client.post(
        f"/api/v1/cms/organizations/{organization.uuid}/packs/{response.data['scenario_id']}/removal/",
        {"pack_digest": response.data["package_digest"]},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert "organization.manage_content" in calls
