"""Tenant plugin administration uses actual scope and independent principals."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.cms.test_runtime_plugins import manifest, worker

pytestmark = [pytest.mark.django_db]
__all__ = ["manifest", "worker"]


@pytest.fixture
def plugin_actor(settings, monkeypatch):
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, resolve_service_credential
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from workspaces.models import Organization
    from workspaces.services import create_account

    accounts = [create_account(kind="team", name=f"Synthetic plugin tenant {index}") for index in range(2)]
    organizations = [Organization.objects.get(account_id=account.id, is_default=True) for account in accounts]
    principal = create_service_principal("Synthetic plugin administrator")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid), issuer="https://accounts.google.com", subject=str(uuid4())
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://example.test/shifter"
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["authorization:organization.manage_adapters"],
    )

    def check(request):
        allowed = request.scope.account_uuid == accounts[0].uuid
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED, "policy_allowed" if allowed else "policy_denied"
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    credential = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    return SimpleNamespace(credential=credential, organization=organizations[0], sibling=organizations[1])


def test_service_administration_preserves_private_tenant_scope_and_installer_proof(plugin_actor, manifest):
    from engine.models import RuntimePluginInstallation
    from engine.services import change_runtime_plugin, install_runtime_plugin, list_runtime_plugins
    from shared.exceptions import ValidationError

    fixture = plugin_actor
    created = install_runtime_plugin(
        fixture.credential,
        fixture.organization.uuid,
        manifest,
        registry_credentials={"username": "synthetic", "password": "SYNTHETIC_PLUGIN_SECRET"},
    )
    row = RuntimePluginInstallation.objects.get(pk=created.id)
    assert row.installed_by_id is None
    assert row.initiating_principal_uuid == fixture.credential.principal.uuid
    assert row.credential_uuid == fixture.credential.credential_uuid
    assert row.authority_account_uuid == fixture.organization.account.uuid
    assert list_runtime_plugins(fixture.credential, fixture.organization.uuid).results[0].id == row.id
    with pytest.raises((ValidationError, ValueError)):
        list_runtime_plugins(fixture.credential, fixture.sibling.uuid)
    assert change_runtime_plugin(fixture.credential, fixture.organization.uuid, row.id, "disable").state == "disabled"


def test_service_binds_qualified_plugin_with_its_own_audit_identity(plugin_actor, manifest):
    from engine.models import RuntimePluginInstallation
    from engine.services import bind_runtime_plugin, install_runtime_plugin, list_runtime_plugin_bindings
    from shared.models import AuditLog
    from shared.runtime_plugin_binding import RuntimePluginScope

    actor = plugin_actor
    installed = install_runtime_plugin(actor.credential, actor.organization.uuid, manifest)
    RuntimePluginInstallation.objects.filter(pk=installed.id).update(state="ready")
    scope = RuntimePluginScope(
        organization_uuid=actor.organization.uuid, pack_id="synthetic", pack_digest="sha256:" + "b" * 64
    )
    binding = bind_runtime_plugin(actor.credential, scope, installed.id, {"targets": {"server": "node.web"}})
    assert list_runtime_plugin_bindings(actor.credential, actor.organization.uuid)[0].id == binding.id
    event = AuditLog.objects.get(entity_ref=str(binding.id), action="update")
    assert event.actor_principal_uuid == actor.credential.principal.uuid
    assert event.actor_id is None


def test_revoked_installation_initiator_cannot_dispatch_a_probe(plugin_actor, manifest, worker):
    from engine.models import RuntimePluginInstallation
    from engine.services import install_runtime_plugin, reconcile_runtime_plugins
    from management.models import ServiceCredentialAdmission

    fixture = plugin_actor
    installed = install_runtime_plugin(fixture.credential, fixture.organization.uuid, manifest)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    reconcile_runtime_plugins()
    assert RuntimePluginInstallation.objects.get(pk=installed.id).state == "failed"
    worker.assert_not_called()


def test_revocation_during_probe_does_not_commit_compatibility_as_ready(plugin_actor, manifest, worker, monkeypatch):
    from shifter_adapter_sdk.runtime import InspectionResult

    from engine.models import RuntimePluginInstallation
    from engine.services import install_runtime_plugin, reconcile_runtime_plugins
    from management.models import ServiceCredentialAdmission

    installed = install_runtime_plugin(plugin_actor.credential, plugin_actor.organization.uuid, manifest)

    def observe(request):
        ServiceCredentialAdmission.objects.filter(uuid=plugin_actor.credential.credential_uuid).update(is_active=False)
        return InspectionResult(
            protocol=request.protocol,
            phase="inspect",
            invocation_id=request.invocation_id,
            input_digest=request.digest,
            status="compatible",
        )

    monkeypatch.setattr("engine.services._runtime_plugin_controller.observe_plugin", observe)
    reconcile_runtime_plugins()
    row = RuntimePluginInstallation.objects.get(pk=installed.id)
    assert row.state == "failed" and row.verified_at is None


@pytest.mark.postgres
@pytest.mark.parametrize(
    "field,value",
    [
        ("credential_uuid", uuid4()),
        ("organization_uuid", uuid4()),
        ("authority_account_uuid", uuid4()),
        ("manifest", {}),
    ],
)
def test_plugin_registration_cannot_rewrite_original_proof_or_executable(plugin_actor, manifest, field, value):
    from django.db import IntegrityError, transaction

    from engine.models import RuntimePluginInstallation
    from engine.services import install_runtime_plugin

    created = install_runtime_plugin(plugin_actor.credential, plugin_actor.organization.uuid, manifest)
    with pytest.raises(IntegrityError), transaction.atomic():
        RuntimePluginInstallation.objects.filter(pk=created.id).update(**{field: value})
