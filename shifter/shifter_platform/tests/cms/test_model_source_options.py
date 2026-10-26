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
