"""The actual organizer discovery endpoint carries a validated content scope."""

import pytest
from rest_framework.test import APIClient

from cms.models import RaesPackageSource
from shared.authorization import AuthorizationDecision, AuthorizationProviderError, DecisionKind
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event

pytestmark = pytest.mark.django_db
URL = "/api/v1/ctf/scenario-options/"


@pytest.fixture
def scenario_discovery(launch_event, organizer_user, organizer_credential, allow_all_policy):
    source = RaesPackageSource.objects.get(scenario_id=launch_event.scenario_id)
    source.is_operator_published = False
    source.save(update_fields=["is_operator_published"])
    RaesPackageSource.objects.create(
        scenario_id="synthetic-shared-catalog",
        source_kind="repo",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref=source.package_ref,
        package_version=source.package_version,
        package_digest=source.package_digest,
        conformance_status="passed",
        scope_kind="installation",
        owner_principal_uuid=organizer_credential.principal.uuid,
        is_operator_published=True,
    )
    client = APIClient()
    client.force_authenticate(user=organizer_user, token=organizer_credential)
    return client, source, allow_all_policy


def test_organizer_scenarios_project_the_explicit_catalog(scenario_discovery):
    client, source, _provider = scenario_discovery
    installation = client.get(URL, {"scope_kind": "installation"})
    assert installation.status_code == 200, installation.content
    assert [row["id"] for row in installation.json()["scenarios"]] == ["synthetic-shared-catalog"]
    account = client.get(URL, {"scope_kind": "account", "account": str(source.account_uuid)})
    assert account.status_code == 200, account.content
    assert {row["id"] for row in account.json()["scenarios"]} == {source.scenario_id, "synthetic-shared-catalog"}
    assert "package_ref" not in account.content.decode()


@pytest.mark.parametrize(
    "query", [{}, {"scope_kind": "account"}, {"scope_kind": "installation", "unknown": "private-canary"}]
)
def test_organizer_scenario_query_is_explicit_and_closed(scenario_discovery, query):
    client, _source, _provider = scenario_discovery
    response = client.get(URL, query)
    assert response.status_code == 400
    assert "private-canary" not in response.content.decode()


@pytest.mark.parametrize("failure", ["denied", "outage", "evaluator_error"])
def test_organizer_scenario_policy_failure_is_bounded(scenario_discovery, monkeypatch, failure):
    client, _source, provider = scenario_discovery

    def check(request):
        if failure == "outage":
            raise AuthorizationProviderError("private-provider-canary")
        return AuthorizationDecision(
            DecisionKind.DENIED if failure == "denied" else DecisionKind.EVALUATOR_ERROR,
            "policy_denied" if failure == "denied" else "evaluator_unavailable",
        )

    monkeypatch.setattr(provider, "check", check)
    response = client.get(URL, {"scope_kind": "installation"})
    assert response.status_code == (403 if failure == "denied" else 503), response.content
    assert "private-provider-canary" not in response.content.decode()
    assert set(response.json()) == {"error"}


def test_native_scenario_options_operation_is_registered():
    from django.urls import resolve

    from ctf.api.operation_policy import API_OPERATION_POLICY
    from ctf.api.organizer.events import ScenarioListView

    assert resolve(URL).func.view_class is ScenarioListView
    assert "ctf_scenario_options_retrieve" in API_OPERATION_POLICY
    assert "ctf_scenarios_retrieve" not in API_OPERATION_POLICY
