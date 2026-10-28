"""Choice discovery admits ordinary tenant members without staff escalation."""

import pytest

from tests.cms.test_model_sources import source_client
from tests.engine.services.test_model_sources import model_source_credential, source_tenant

__all__ = ["source_tenant"]

pytestmark = pytest.mark.django_db


def test_explicit_policy_grant_allows_only_its_own_workspace_scope(source_tenant):
    from workspaces.models import Account, Organization, Workspace

    _, member, org, _ = source_tenant
    member.grant_model_sources()
    client = source_client(member)
    own = Workspace.objects.get(organization=org)
    response = client.get("/api/v1/cms/model-source-options/", {"workspace": str(own.uuid)})
    assert response.status_code == 200
    assert {str(item["uuid"]) for item in response.data["workspaces"]} == {str(own.uuid)}
    foreign_account = Account.objects.create(kind=Account.Kind.TEAM, name="Foreign")
    foreign = Workspace.objects.create(
        name="Foreign", organization=Organization.objects.create(name="Foreign", account=foreign_account)
    )
    denied = client.get("/api/v1/cms/model-source-options/", {"workspace": str(foreign.uuid)})
    assert denied.status_code == 403
    assert "Foreign" not in str(denied.data)


def test_picker_hides_sources_outside_authored_capability_and_input_bounds(source_tenant, settings, monkeypatch):
    from uuid import uuid4

    from cms.scenarios.model_needs import ScenarioModelNeedsProjection
    from cms.services import model_source_alias_options
    from engine.services import create_model_source
    from tests.engine.services.test_model_admission import _need
    from tests.engine.services.test_model_sources import configuration, direct_source_catalog

    admin, member, org, _ = source_tenant
    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = direct_source_catalog(uuid4())
    need = _need().model_copy(
        update={"data_regions": ("provider-managed",), "required_capabilities": ("messages", "token-count")}
    )
    monkeypatch.setattr("cms.scenarios.registry.check_scenario_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "cms.scenarios.model_needs.project_scenario_model_needs",
        lambda _: ScenarioModelNeedsProjection(needs={"participant": need}, digest_verified=True),
    )
    create_model_source(
        model_source_credential(admin),
        org.uuid,
        configuration(provider="openrouter-v1", upstream_provider="Synthetic"),
        credential={"api_key": "synthetic-secret"},
    )
    member.grant_model_sources()
    aliases, _ = model_source_alias_options(model_source_credential(member), org.uuid, "synthetic")
    assert aliases and not aliases[0]["sources"]
    need = need.model_copy(update={"required_capabilities": ("messages",)})
    aliases, _ = model_source_alias_options(model_source_credential(member), org.uuid, "synthetic")
    assert not aliases[0]["sources"]  # no undocumented token count or weaker input ceiling


def test_workspace_owned_scenario_is_available_to_model_source_options(source_tenant, settings, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from rest_framework.test import APIClient

    from cms.models import RaesPackageSource
    from cms.scenarios.model_needs import ScenarioModelNeedsProjection
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from tests.engine.services.test_model_sources import direct_source_catalog
    from workspaces.models import Workspace

    admin, member, organization, _ = source_tenant
    workspace = Workspace.objects.get(organization=organization)
    RaesPackageSource.objects.create(
        scenario_id="synthetic-workspace-model",
        registered_by=admin,
        source_kind="repo",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="packs/synthetic",
        package_version="1",
        package_digest="sha256:" + "1" * 64,
        conformance_status="passed",
        scope_kind="account",
        account_uuid=organization.account.uuid,
        organization_uuid=organization.uuid,
        workspace_uuid=workspace.uuid,
        owner_principal_uuid=member.identity_principal.uuid,
    )
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    monkeypatch.setattr(
        "cms.scenarios.model_needs.project_scenario_model_needs",
        lambda _: ScenarioModelNeedsProjection(needs={}, digest_verified=True),
    )
    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = direct_source_catalog(uuid4())
    client = APIClient()
    client.force_login(member)
    response = client.get(
        "/api/v1/cms/model-source-options/",
        {
            "workspace": str(workspace.uuid),
            "scenario": "synthetic-workspace-model",
        },
    )
    assert response.status_code == 200
    assert response.json()["available"] is True


@pytest.mark.parametrize("outage", [False, True])
def test_model_admin_without_content_read_gets_bounded_domain_error(source_tenant, settings, monkeypatch, outage):
    from types import SimpleNamespace
    from uuid import uuid4

    from rest_framework.test import APIClient

    from cms.models import RaesPackageSource
    from shared.authorization import configured_authorization_provider, port
    from tests.engine.services.test_model_sources import direct_source_catalog
    from workspaces.models import Workspace

    admin, member, organization, _ = source_tenant
    workspace = Workspace.objects.get(organization=organization)
    member.grant_model_sources()
    RaesPackageSource.objects.create(
        scenario_id="synthetic-denied-model-content",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="synthetic/archive",
        package_version="1",
        package_digest="sha256:" + "1" * 64,
        conformance_status="passed",
        scope_kind="account",
        account_uuid=organization.account.uuid,
        organization_uuid=organization.uuid,
        workspace_uuid=workspace.uuid,
        owner_principal_uuid=admin.identity_principal.uuid,
    )
    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = direct_source_catalog(uuid4())
    provider = configured_authorization_provider()
    seen = []

    def check(request):
        seen.append(request.action)
        if outage and request.action.endswith(".read_content"):
            raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        return provider.check(request)

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    client = APIClient()
    client.force_login(member)
    response = client.get(
        "/api/v1/cms/model-source-options/",
        {
            "workspace": str(workspace.uuid),
            "scenario": "synthetic-denied-model-content",
        },
    )
    assert response.status_code == (503 if outage else 403), response.data
    assert response.data["error"]["code"] == "source_options_unavailable"
    assert "organization.manage_model_sources" in seen and "workspace.read_content" in seen
    assert "SYNTHETIC_PRIVATE_PROVIDER_ERROR" not in str(response.data)
