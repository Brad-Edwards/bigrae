"""The uniform registration boundary admits independent content administrators."""

from dataclasses import replace

import pytest

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from cms.services import PackRegistrationRequest, register_pack
from tests.cms.services.test_principal_content import content_actor

pytestmark = [pytest.mark.django_db]
__all__ = ["content_actor"]


@pytest.fixture
def content_administrator(content_actor, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from shared.authorization import AuthorizationDecision, DecisionKind, configured_authorization_provider, port

    row = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=content_actor.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=row.binding.principal, issuer=row.binding.issuer, subject=str(uuid4())
    )
    row = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=row.audience,
        scopes=[
            *row.scopes,
            "authorization:account.manage_content",
            "authorization:account.publish_content",
            "authorization:installation.manage_content",
        ],
    )
    content_actor.credential = resolve_service_credential(
        issuer=row.binding.issuer, subject=row.binding.subject, audience=row.audience
    )
    provider = configured_authorization_provider()

    def check(request):
        # This fixture imports generic references into a private account. That
        # requires a separate installation content grant, not an archive guess.
        if request.scope.kind == "installation" and request.action == "installation.manage_content":
            return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")
        return provider.check(request)

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    return content_actor


def registration():
    return PackRegistrationRequest(
        scenario_id="synthetic-principal-pack",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="synthetic/pack-v1",
        package_version="1",
        package_digest="sha256:" + "a" * 64,
    )


def test_service_registration_revision_and_retry_keep_private_custody(content_administrator):
    fixture = content_administrator
    request = registration()
    result = register_pack(user=fixture.credential, request=request, scope=fixture.scope)
    row = RaesPackageSource.objects.get(scenario_id=result.scenario_id)
    assert row.registered_by_id is None
    assert row.owner_principal_uuid == fixture.credential.principal.uuid
    assert row.account_uuid == fixture.scope.account_uuid
    assert row.scope_kind == "account"
    assert not row.is_operator_published
    assert row.conformance_status == "pending"
    from cms.scenarios.registry import list_all_scenarios

    assert list_all_scenarios() == []
    assert not register_pack(user=fixture.credential, request=request, scope=fixture.scope, idempotent=True).created
    revised = replace(
        request,
        package_ref="synthetic/pack-v2",
        package_version="2",
        package_digest="sha256:" + "b" * 64,
        expected_package_digest=request.package_digest,
    )
    assert not register_pack(user=fixture.credential, request=revised, scope=fixture.scope).created
    row.refresh_from_db()
    assert row.package_digest == revised.package_digest
    assert row.account_uuid == fixture.scope.account_uuid
    assert row.owner_principal_uuid == fixture.credential.principal.uuid


def test_read_only_service_cannot_register_content(content_actor):
    with pytest.raises(CMSError):
        register_pack(user=content_actor.credential, request=registration(), scope=content_actor.scope)
    assert not RaesPackageSource.objects.exists()


def test_service_metadata_change_uses_native_private_scope_and_principal_audit(content_administrator):
    from cms.scenario_editor._metadata import update_metadata
    from shared.models import AuditLog

    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    metadata = update_metadata(fixture.credential, registration().scenario_id, enabled=False)
    assert metadata.enabled is False
    assert metadata.updated_by_id is None
    assert metadata.updated_by_principal_uuid == fixture.credential.principal.uuid
    assert AuditLog.objects.filter(
        actor_principal_uuid=fixture.credential.principal.uuid,
        entity_ref=registration().scenario_id,
        action="update",
    ).exists()


def test_metadata_revoked_during_policy_cannot_write(content_administrator, monkeypatch):
    from types import SimpleNamespace

    from cms.models import ScenarioMetadata
    from cms.scenario_editor._metadata import update_metadata
    from management.models import ServiceCredentialAdmission
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    checks = []

    def check(request):
        checks.append(request)
        if len(checks) == 2:
            ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    with pytest.raises(CMSError):
        update_metadata(fixture.credential, registration().scenario_id, enabled=False)
    assert not ScenarioMetadata.objects.exists()


def test_metadata_revoked_after_admission_cannot_write(content_administrator, monkeypatch):
    from cms.models import ScenarioMetadata
    from cms.scenario_editor import _scoped_metadata
    from cms.scenario_editor._metadata import update_metadata
    from management.models import ServiceCredentialAdmission

    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    original = _scoped_metadata.require_content_scope
    calls = 0

    def admit_then_revoke(*args, **kwargs):
        nonlocal calls
        current = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            ServiceCredentialAdmission.objects.filter(uuid=current.credential_uuid).update(is_active=False)
        return current

    monkeypatch.setattr(_scoped_metadata, "require_content_scope", admit_then_revoke)
    with pytest.raises(CMSError):
        update_metadata(fixture.credential, registration().scenario_id, enabled=False)
    assert calls == 2
    assert not ScenarioMetadata.objects.exists()


def test_raw_staff_user_cannot_bypass_native_metadata_authority(content_administrator, django_user_model):
    from cms.models import ScenarioMetadata
    from cms.scenario_editor._metadata import update_metadata

    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    staff = django_user_model.objects.create_superuser(username="synthetic-metadata-staff", password="unused")
    with pytest.raises(CMSError):
        update_metadata(staff, registration().scenario_id, enabled=False)
    assert not ScenarioMetadata.objects.exists()


def test_metadata_audit_failure_rolls_back_native_overlay(content_administrator, monkeypatch):
    from cms.models import ScenarioMetadata
    from cms.scenario_editor._metadata import update_metadata

    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.scenario_editor._scoped_metadata.audit_log", unavailable)
    with pytest.raises(CMSError):
        update_metadata(fixture.credential, registration().scenario_id, enabled=False)
    assert not ScenarioMetadata.objects.exists()


def test_scoped_registration_cannot_rehome_existing_source(content_administrator, monkeypatch):
    fixture = content_administrator
    register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    # Even a policy grant in both customers cannot reinterpret an existing ID.
    from types import SimpleNamespace

    from shared.authorization import AuthorizationDecision, DecisionKind, port

    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    with pytest.raises(CMSError):
        register_pack(user=fixture.credential, request=registration(), scope=fixture.sibling, idempotent=True)
    assert RaesPackageSource.objects.get().account_uuid == fixture.scope.account_uuid


def test_source_registration_strict_audit_failure_rolls_back(content_administrator, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.services._content_ingestion.audit_log", unavailable)
    with pytest.raises(CMSError):
        register_pack(user=content_administrator.credential, request=registration(), scope=content_administrator.scope)
    assert not RaesPackageSource.objects.exists()


def test_registration_revocation_during_validation_prevents_write(content_administrator, monkeypatch):
    from cms.services import _content_ingestion
    from management.models import ServiceCredentialAdmission

    validate = _content_ingestion._bind_scenario_id_to_pack_identity

    def validated_then_revoked(request):
        validate(request)
        ServiceCredentialAdmission.objects.filter(uuid=content_administrator.credential.credential_uuid).update(
            is_active=False
        )

    monkeypatch.setattr(_content_ingestion, "_bind_scenario_id_to_pack_identity", validated_then_revoked)
    with pytest.raises(CMSError):
        register_pack(user=content_administrator.credential, request=registration(), scope=content_administrator.scope)
    assert not RaesPackageSource.objects.exists()


@pytest.mark.parametrize("revision", [False, True])
def test_registration_revoked_after_final_admission_cannot_mutate(content_administrator, monkeypatch, revision):
    from cms.services import _content_ingestion
    from management.models import ServiceCredentialAdmission

    fixture = content_administrator
    original_request = registration()
    request = original_request
    if revision:
        register_pack(user=fixture.credential, request=request, scope=fixture.scope)
        request = replace(
            request,
            package_ref="synthetic/pack-v2",
            package_version="2",
            package_digest="sha256:" + "b" * 64,
            expected_package_digest=original_request.package_digest,
        )
    original = _content_ingestion._authorize_registration
    calls = 0

    def admit_then_revoke(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 3:
            ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
        return result

    monkeypatch.setattr(_content_ingestion, "_authorize_registration", admit_then_revoke)
    with pytest.raises((CMSError, ValueError)):
        register_pack(user=fixture.credential, request=request, scope=fixture.scope)
    assert calls == 3
    if revision:
        assert RaesPackageSource.objects.get().package_digest == original_request.package_digest
    else:
        assert not RaesPackageSource.objects.exists()


def test_customer_source_cannot_be_published_as_operator_catalog(content_administrator):
    from cms.services._content_authority import set_catalog_publication

    fixture = content_administrator
    result = register_pack(user=fixture.credential, request=registration(), scope=fixture.scope)
    with pytest.raises(CMSError):
        set_catalog_publication(fixture.credential, result.scenario_id, published=True)
    assert not RaesPackageSource.objects.get().is_operator_published


def test_operator_publication_and_unpublication_control_shared_projection(content_actor, monkeypatch, settings):
    from types import SimpleNamespace
    from uuid import uuid4

    from cms.scenarios.registry import list_all_scenarios
    from cms.services import set_catalog_publication
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from shared.identity_scope import ResourceScope

    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=content_actor.credential.principal.uuid),
        issuer="https://accounts.google.com",
        subject=str(uuid4()),
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["authorization:installation.manage_content", "authorization:installation.publish_content"],
    )
    operator = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    result = register_pack(user=operator, request=registration(), scope=ResourceScope("installation"))
    assert list_all_scenarios(content_actor.credential, scope=content_actor.scope) == []
    set_catalog_publication(operator, result.scenario_id, published=True)
    entries = list_all_scenarios(content_actor.credential, scope=content_actor.scope)
    assert [entry["id"] for entry in entries] == [result.scenario_id]
    assert "synthetic/pack-v1" not in repr(entries)
    set_catalog_publication(operator, result.scenario_id, published=False)
    assert list_all_scenarios(content_actor.credential, scope=content_actor.scope) == []


def test_publication_revoked_after_admission_cannot_write(content_actor, monkeypatch, settings, allow_all_policy):
    from uuid import uuid4

    from cms.services import _content_authority
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from shared.identity_scope import ResourceScope

    prior = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=content_actor.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=prior.binding.principal, issuer=prior.binding.issuer, subject=str(uuid4())
    )
    ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=prior.audience,
        scopes=["authorization:installation.manage_content", "authorization:installation.publish_content"],
    )
    actor = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=prior.audience)
    result = register_pack(user=actor, request=registration(), scope=ResourceScope("installation"))
    original = _content_authority.require_content_scope

    def admit_then_revoke(*args, **kwargs):
        current = original(*args, **kwargs)
        ServiceCredentialAdmission.objects.filter(uuid=current.credential_uuid).update(is_active=False)
        return current

    monkeypatch.setattr(_content_authority, "require_content_scope", admit_then_revoke)
    with pytest.raises((CMSError, ValueError)):
        _content_authority.set_catalog_publication(actor, result.scenario_id, published=True)
    assert not RaesPackageSource.objects.get(scenario_id=result.scenario_id).is_operator_published
