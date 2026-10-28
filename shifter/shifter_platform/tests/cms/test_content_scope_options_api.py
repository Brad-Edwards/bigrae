"""The actual selector route offers read_content grants without parent directory permissions."""

from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from shared.authorization import AuthorizationDecision, DecisionKind, port
from tests.cms.test_runtime_plugins import tenant

__all__ = ["tenant"]
pytestmark = pytest.mark.django_db


@pytest.fixture(params=["human", "service"])
def selector(tenant, request, settings, monkeypatch):
    from cms.models import RaesPackageSource
    from tests.cms._warm_pool_fixtures import service_credential

    user, organization = tenant
    credential = (
        user.credential
        if request.param == "human"
        else service_credential(
            settings, scopes=["authorization:organization.read_content", "authorization:workspace.read_content"]
        )
    )
    client = APIClient()
    client.force_authenticate(user=user if request.param == "human" else None, token=credential)
    grants = {("organization.read_content", organization.uuid)}
    seen = []

    def check(check_request):
        seen.append(check_request)
        allowed = (
            check_request.principal == credential.principal
            and (check_request.action, check_request.target.uuid) in grants
        )
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED, "policy_allowed" if allowed else "policy_denied"
        )

    provider = SimpleNamespace(check=check, batch_check=lambda requests: tuple(check(item) for item in requests))
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    source = RaesPackageSource.objects.create(
        scenario_id="synthetic-scope-option",
        contract_kind="raes",
        contract_profile="shifter",
        source_kind="object",
        package_ref="synthetic/content.tar.gz",
        package_version="1",
        package_digest="sha256:" + "a" * 64,
        conformance_status="passed",
        owner_principal_uuid=user.credential.principal.uuid,
        scope_kind="account",
        account_uuid=organization.account.uuid,
        organization_uuid=organization.uuid,
    )
    return SimpleNamespace(
        client=client,
        credential=credential,
        organization=organization,
        grants=grants,
        seen=seen,
        source=source,
        provider=provider,
    )


def test_selector_and_catalog_read_need_only_exact_content_action(selector):
    actor = selector
    response = actor.client.get("/api/v1/cms/content-scopes/")
    assert response.status_code == 200, response.data
    assert response.data == {
        "count": 1,
        "results": [
            {
                "kind": "organization",
                "name": actor.organization.name,
                "scope_kind": "account",
                "account": str(actor.organization.account.uuid),
                "organization": str(actor.organization.uuid),
                "workspace": None,
            }
        ],
    }
    option = response.data["results"][0]
    response = actor.client.get(
        "/api/v1/cms/catalog/",
        {
            key: value
            for key, value in option.items()
            if key in {"scope_kind", "account", "organization", "workspace"} and value is not None
        },
    )
    assert response.status_code == 200, response.data
    assert [row["id"] for row in response.data] == [actor.source.scenario_id]
    assert all(row.action.endswith(".read_content") for row in actor.seen)


def test_selector_counts_and_pages_only_authorized_leaf_names(selector):
    from workspaces.models import Workspace

    actor = selector
    for index in range(3):
        workspace = Workspace.objects.create(organization=actor.organization, name=f"Content team {index}")
        if index != 1:
            actor.grants.add(("workspace.read_content", workspace.uuid))
    response = actor.client.get("/api/v1/cms/content-scopes/", {"offset": 1, "limit": 1})
    assert response.status_code == 200, response.data
    assert response.data["count"] == 3
    assert len(response.data["results"]) == 1
    row = response.data["results"][0]
    assert row["kind"] == "workspace" and row["name"] == "Content team 0"
    assert set(row) == {"kind", "name", "scope_kind", "account", "organization", "workspace"}
    assert row["account"] == str(actor.organization.account.uuid)
    assert row["organization"] == str(actor.organization.uuid)


def test_selector_role_flags_do_not_supply_missing_policy(selector, tenant):
    from workspaces.models import OrganizationMembership

    actor = selector
    actor.grants.clear()
    user = tenant[0]
    user.is_staff = user.is_superuser = True
    user.save(update_fields=["is_staff", "is_superuser"])
    OrganizationMembership.objects.create(user=user, organization=actor.organization, role="admin")
    response = actor.client.get("/api/v1/cms/content-scopes/")
    assert response.status_code == 200 and response.data == {"count": 0, "results": []}


@pytest.mark.parametrize("query", [{"limit": 101}, {"offset": -1}, {"action": "organization.read"}])
def test_selector_closed_paging_rejects_unbounded_or_changed_action(selector, query):
    assert selector.client.get("/api/v1/cms/content-scopes/", query).status_code == 400


def test_selector_does_not_return_an_admission_revoked_during_policy(selector, monkeypatch):
    from management.models import Principal

    original = selector.provider.batch_check

    def allowed_then_revoke(requests):
        decisions = original(requests)
        Principal.objects.filter(uuid=selector.credential.principal.uuid).update(is_active=False)
        return decisions

    monkeypatch.setattr(selector.provider, "batch_check", allowed_then_revoke)
    response = selector.client.get("/api/v1/cms/content-scopes/")
    assert response.status_code == 403
    assert "results" not in response.data
