"""Scoped registration cannot borrow another tenant's actual uploaded archive."""

from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from tests.cms.test_tenant_pack_upload import _post, tenant, upload

__all__ = ["tenant", "upload"]
pytestmark = pytest.mark.django_db


def _request(source):
    from cms.services import PackRegistrationRequest

    return PackRegistrationRequest(
        **{
            name: getattr(source, name)
            for name in (
                "scenario_id",
                "package_name",
                "source_kind",
                "contract_kind",
                "contract_profile",
                "package_ref",
                "package_version",
                "package_digest",
                "lock_ref",
                "lock_digest",
                "provenance",
            )
        }
    )


def _credential(tenant, settings, service):
    if not service:
        return tenant[0].credential
    from tests.cms._warm_pool_fixtures import service_credential

    credential = service_credential(settings, scopes=["authorization:organization.manage_content"])
    tenant[0].policy_principals.add(credential.principal.uuid)
    return credential


@pytest.fixture
def installed(upload):
    from cms.models import RaesPackageSource

    client, organization, root, storage = upload
    response = _post(client, organization, root)
    assert response.status_code == 201, response.data
    return RaesPackageSource.objects.get(), organization, storage


@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("operation", ["create", "revise"])
def test_actual_http_registration_refuses_foreign_uploaded_bytes(
    installed, upload, tenant, settings, service, operation
):
    from cms.models import RaesPackageSource
    from workspaces.models import Organization
    from workspaces.services import create_account

    victim, _, storage = installed
    client, _, root, _ = upload
    account = create_account(kind="team", name="Independent content destination")
    destination = Organization.objects.get(account_id=account.id, is_default=True)
    tenant[0].policy_accounts.add(account.uuid)
    credential = _credential(tenant, settings, service)
    client.force_authenticate(user=None if service else tenant[0], token=credential)
    request = replace(_request(victim), scenario_id=f"synthetic-alias-{uuid4().hex}")
    expected = ""
    target = None
    if operation == "revise":
        response = _post(client, destination, root)
        assert response.status_code == 201, response.data
        target = RaesPackageSource.objects.exclude(pk=victim.pk).get()
        request = replace(request, scenario_id=target.scenario_id, package_version="2")
        expected = target.package_digest
    body = asdict(request)
    body.pop("package_name")
    body.update(
        scope_kind="account",
        account=str(account.uuid),
        organization=str(destination.uuid),
        expected_package_digest=expected,
    )
    stored = dict(storage.objects)
    response = client.post("/api/v1/cms/catalog/packs/", body, format="json")
    assert response.status_code == 403, response.data
    assert storage.objects == stored
    assert RaesPackageSource.objects.count() == (2 if target else 1)
    if target:
        target.refresh_from_db()
        assert target.package_ref != victim.package_ref and target.package_digest == expected


@pytest.mark.parametrize("service", [False, True])
def test_direct_idempotent_registration_refuses_foreign_archive(installed, tenant, settings, service):
    from cms.exceptions import CMSError
    from cms.services import register_pack
    from workspaces.models import Organization
    from workspaces.services import create_account, hierarchy_target_scope

    source, _, _ = installed
    account = create_account(kind="team", name="Independent retry destination")
    destination = Organization.objects.get(account_id=account.id, is_default=True)
    tenant[0].policy_accounts.add(account.uuid)
    with pytest.raises(CMSError):
        register_pack(
            user=_credential(tenant, settings, service),
            request=_request(source),
            scope=hierarchy_target_scope("organization", destination.uuid),
            idempotent=True,
        )


def test_actual_uploaded_archive_allows_native_own_scope_retry(installed, tenant):
    from cms.services import register_pack
    from workspaces.services import hierarchy_target_scope

    source, organization, _ = installed
    assert not register_pack(
        user=tenant[0].credential,
        request=_request(source),
        scope=hierarchy_target_scope("organization", organization.uuid),
        idempotent=True,
    ).created


@pytest.mark.parametrize("operation", ["create", "revise", "retry"])
@pytest.mark.parametrize("changed", ["organization_uuid", "object_key", "location_verified", "removal"])
def test_prepared_reference_rechecks_recorded_custody_before_every_commit(installed, tenant, operation, changed):
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, TenantPackArchive, TenantPackRemoval
    from cms.services import register_pack
    from cms.services._content_ingestion import prepare_pack_registration
    from workspaces.services import hierarchy_target_scope

    source, organization, _ = installed
    request = _request(source)
    if operation == "create":
        source.delete()
    elif operation == "revise":
        request = replace(request, expected_package_digest=source.package_digest, package_version="2")
    scope = hierarchy_target_scope("organization", organization.uuid)
    admission = prepare_pack_registration(tenant[0].credential, request, scope)
    archive = TenantPackArchive.objects.get(package_ref=request.package_ref)
    value = {"organization_uuid": uuid4(), "object_key": "foreign/key", "location_verified": False}.get(changed)
    if changed == "removal":
        from django.utils import timezone

        value = TenantPackRemoval.objects.create(
            organization_uuid=organization.uuid,
            scenario_id=source.scenario_id,
            package_name=source.package_name,
            package_version=source.package_version,
            package_digest=source.package_digest,
            next_attempt_at=timezone.now(),
        )
    setattr(archive, changed, value)
    archive.save(update_fields=[changed])
    with pytest.raises(CMSError):
        register_pack(
            user=tenant[0].credential,
            request=request,
            scope=scope,
            idempotent=operation == "retry",
            _prepared_admission=admission,
        )
    if operation == "create":
        assert not RaesPackageSource.objects.exists()
    else:
        source.refresh_from_db()
        assert source.package_version != "2"


def test_inflight_uploaded_bytes_require_original_journal_lease(upload, tenant, monkeypatch):
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, TenantPackArchive
    from cms.services import PackRegistrationRequest, register_pack
    from workspaces.services import hierarchy_target_scope

    client, organization, root, storage = upload
    write = storage.upload_file
    refused = []

    def store_then_try_to_borrow(*args, **kwargs):
        write(*args, **kwargs)
        archive = TenantPackArchive.objects.select_related("removal").get()
        journal = archive.removal
        request = PackRegistrationRequest(
            scenario_id=journal.scenario_id,
            package_name=journal.package_name,
            source_kind="object",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref=archive.package_ref,
            package_version=journal.package_version,
            package_digest=journal.package_digest,
        )
        with pytest.raises(CMSError):
            register_pack(
                user=tenant[0].credential,
                request=request,
                scope=hierarchy_target_scope("organization", organization.uuid),
            )
        refused.append(True)
        assert not RaesPackageSource.objects.exists()

    monkeypatch.setattr(storage, "upload_file", store_then_try_to_borrow)
    response = _post(client, organization, root)
    assert response.status_code == 201, response.data
    assert refused and RaesPackageSource.objects.count() == 1


def test_uninventoried_reference_requires_separate_installation_grant(installed, tenant, settings, monkeypatch):
    from types import SimpleNamespace

    from cms.exceptions import CMSError
    from cms.services import register_pack
    from shared.authorization import AuthorizationDecision, DecisionKind, configured_authorization_provider, port
    from tests.cms._warm_pool_fixtures import service_credential
    from workspaces.services import hierarchy_target_scope

    source, organization, _ = installed
    request = replace(
        _request(source),
        scenario_id=f"generic-{uuid4().hex}",
        package_name="synthetic-import",
        package_ref="external/catalog/archive",
    )
    scope = hierarchy_target_scope("organization", organization.uuid)
    with pytest.raises(CMSError):
        register_pack(user=tenant[0].credential, request=request, scope=scope)
    operator = service_credential(
        settings,
        scopes=[
            "authorization:organization.manage_content",
            "authorization:installation.manage_content",
        ],
    )
    tenant[0].policy_principals.add(operator.principal.uuid)
    existing = configured_authorization_provider()

    def check(request):
        if request.scope.kind == "installation":
            allowed = request.principal == operator.principal and request.action == "installation.manage_content"
            return AuthorizationDecision(
                DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
                "policy_allowed" if allowed else "policy_denied",
            )
        return existing.check(request)

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    assert register_pack(user=operator, request=request, scope=scope).created


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_concurrent_removal_cannot_be_undone_by_a_prepared_registration_retry(installed, tenant):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import connection

    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, TenantPackArchive
    from cms.services import register_pack, request_tenant_pack_removal
    from cms.services._content_ingestion import prepare_pack_registration
    from workspaces.services import hierarchy_target_scope

    source, organization, _ = installed
    credential, request = tenant[0].credential, _request(source)
    scope = hierarchy_target_scope("organization", organization.uuid)
    prepared, removed = Event(), Event()

    def retry():
        try:
            admission = prepare_pack_registration(credential, request, scope)
            prepared.set()
            assert removed.wait(15)
            with pytest.raises(CMSError):
                register_pack(
                    user=credential, request=request, scope=scope, idempotent=True, _prepared_admission=admission
                )
        finally:
            connection.close()

    def remove():
        try:
            assert prepared.wait(15)
            request_tenant_pack_removal(credential, organization.uuid, source.scenario_id, source.package_digest)
            removed.set()
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(retry), executor.submit(remove)]
        for future in futures:
            future.result(timeout=30)
    assert not RaesPackageSource.objects.exists()
    assert not TenantPackArchive.objects.exists()
