"""Account-administration HTTP integration for the coordinated S8 activation."""

from uuid import uuid4

import pytest
from django.urls import resolve
from rest_framework.test import APIClient

from tests.workspaces.test_account_policy_admin import Provider, _actor
from workspaces import services
from workspaces.models import Organization, Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def test_account_administration_routes_are_mounted_in_production():
    match = resolve("/api/v1/accounts/")

    assert match.route == "api/v1/accounts/"


@pytest.mark.parametrize("integration", ["adapters", "model_sources"])
@pytest.mark.parametrize("authority", ["organization", "installation"])
def test_integration_discovery_and_bootstrap_admit_organization_only_administrators(
    monkeypatch, integration, authority
):
    action = f"{authority}.manage_{integration}"
    actor = _actor("service", actions=frozenset({action}))
    account = services.create_account(kind="team", name="Visible account")
    organization = Organization.objects.get(account_id=account.id)
    services.create_account(kind="team", name="Other account")
    provider = Provider(allowed_targets=frozenset({organization.uuid if authority == "organization" else None}))

    def authenticate(_self, request):
        request.credential_context = actor
        return None, actor

    monkeypatch.setattr("shared.api_tokens.authentication.ApiTokenAuthentication.authenticate", authenticate)
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer admitted-service")
    response = client.get("/api/v1/administer/organizations/", {"integration": integration, "limit": 1})
    assert response.status_code == 200
    assert response.data["count"] == (1 if authority == "organization" else 2)
    assert response.data["results"][0]["uuid"] == str(organization.uuid)
    bootstrap = client.get("/api/v1/bootstrap/")
    assert bootstrap.status_code == 200
    assert bootstrap.data["permissions"][f"can_manage_{integration}"] is True
    assert bootstrap.data["permissions"]["can_administer_platform"] is (authority == "installation")
    assert all(request.action == action for request in provider.requests)


@pytest.mark.parametrize("creation_kind", ["account", "organization", "workspace"])
def test_failed_hierarchy_creation_returns_a_receipt_and_retries_the_same_object(monkeypatch, creation_kind):
    from shared.authorization import ACTION_CATALOG
    from workspaces.models import Account, HierarchyPublication

    class RecoverableProvider(Provider):
        failing = True

        def write_relationships(self, change):
            if self.failing:
                raise TimeoutError
            super().write_relationships(change)

    actor = _actor("service", actions=frozenset(item.code for item in ACTION_CATALOG))
    provider = RecoverableProvider()
    endpoint = "/api/v1/accounts/"
    model = Account
    body = {"kind": "team", "name": "Recoverable object"}
    if creation_kind != "account":
        provider.failing = False
        parent = services.admin_create_account(actor, provider, kind="team", name="Parent account")
        organization = Organization.objects.get(account_id=parent.id)
        endpoint = f"/api/v1/accounts/{parent.uuid}/organizations/"
        model = Organization
        body = {"name": "Recoverable object"}
        if creation_kind == "workspace":
            endpoint += f"{organization.uuid}/workspaces/"
            model = Workspace
        provider.failing = True

    def authenticate(_self, request):
        request.credential_context = actor
        return None, actor

    monkeypatch.setattr("shared.api_tokens.authentication.ApiTokenAuthentication.authenticate", authenticate)
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer admitted-service")
    response = client.post(endpoint, body, format="json")
    assert response.status_code == 202
    object_uuid = model.objects.get(name="Recoverable object").uuid
    assert response.data["uuid"] == str(object_uuid)
    assert response.data["publication_state"] == "pending"
    retry_body = {**body, "publication_receipt": response.data["publication_receipt"]}
    # The receipt cannot be repurposed for another declaration, even by an administrator.
    assert client.post(endpoint, {**retry_body, "name": "Changed"}, format="json").status_code == 403
    pending = client.post(endpoint, retry_body, format="json")
    assert pending.status_code == 202
    assert pending.data == response.data
    provider.failing = False
    confirmed = client.post(endpoint, retry_body, format="json")
    assert confirmed.status_code == 201
    assert confirmed.data["uuid"] == str(object_uuid)
    replay = client.post(endpoint, retry_body, format="json")
    assert replay.status_code == 201
    assert replay.data["uuid"] == str(object_uuid)
    assert model.objects.get(name="Recoverable object").uuid == object_uuid
    assert (
        HierarchyPublication.objects.filter(state="confirmed").count()
        == {"account": 3, "organization": 5, "workspace": 4}[creation_kind]
    )
    assert not HierarchyPublication.objects.exclude(state="confirmed").exists()
    assert model.objects.filter(name="Recoverable object").count() == 1


@pytest.mark.parametrize("kind", ["human", "service"])
def test_production_http_profile_membership_and_quota_operations(monkeypatch, kind):
    from django.contrib.auth import get_user_model

    actor = _actor(
        kind, actions=frozenset({"account.update", "organization.update", "workspace.manage_members", "workspace.read"})
    )
    account = services.create_account(kind="team", name="Team")
    organization = Organization.objects.get(account_id=account.id)
    workspace = Workspace.objects.get(organization=organization)
    subject = _actor("service").principal
    client = APIClient()
    if kind == "human":
        client.force_login(
            get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid),
            backend="config.auth.PlatformModelBackend",
        )
    else:

        def authenticate(_self, request):
            request.credential_context = actor
            return None, actor

        monkeypatch.setattr("shared.api_tokens.authentication.ApiTokenAuthentication.authenticate", authenticate)
        client.credentials(HTTP_AUTHORIZATION="Bearer admitted-service")
    monkeypatch.setattr("shared.authorization.port._provider_factory", Provider)
    response = client.patch(f"/api/v1/accounts/{account.uuid}/", {"name": "New name"}, format="json")
    assert response.status_code == 200
    response = client.patch(
        f"/api/v1/accounts/{account.uuid}/organizations/{organization.uuid}/",
        {"description": "Division"},
        format="json",
    )
    assert response.status_code == 200
    response = client.post(
        f"/api/v1/workspaces/{workspace.uuid}/memberships/",
        {"principal_uuid": str(subject.uuid)},
        format="json",
    )
    assert response.status_code == 204
    assert WorkspaceMembership.objects.filter(workspace=workspace, principal_uuid=subject.uuid).exists()
    response = client.get(f"/api/v1/workspaces/{workspace.uuid}/quota/")
    assert response.status_code == 200 and response.data["workspace_uuid"] == str(workspace.uuid)
    response = client.get(f"/api/v1/accounts/{account.uuid}/organizations/{organization.uuid}/workspaces/")
    assert response.status_code == 200 and response.data["results"][0]["uuid"] == str(workspace.uuid)
    assert (
        client.post(
            f"/api/v1/workspaces/{workspace.uuid}/memberships/",
            {"principal_uuid": str(uuid4()), "role": "owner"},
            format="json",
        ).status_code
        == 400
    )


def test_account_admin_contract_matches_active_routes():
    import json
    from pathlib import Path

    from drf_spectacular.generators import SchemaGenerator

    expected = json.loads(Path("openapi/account-admin-prepared.json").read_text())
    schema = SchemaGenerator(urlconf="config.account_admin_urls").get_schema(public=True)
    assert json.loads(json.dumps(schema)) == expected


@pytest.mark.parametrize("kind", ["human", "service"])
def test_production_http_retained_workspace_commands_use_principal_facts(monkeypatch, kind):
    from django.contrib.auth import get_user_model

    from shared.authorization import ACTION_CATALOG

    actor = _actor(kind, actions=frozenset(item.code for item in ACTION_CATALOG))
    subject = _actor("service").principal
    client = APIClient()
    if kind == "human":
        client.force_login(
            get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid),
            backend="config.auth.PlatformModelBackend",
        )
    else:

        def authenticate(_self, request):
            request.credential_context = actor
            return None, actor

        monkeypatch.setattr("shared.api_tokens.authentication.ApiTokenAuthentication.authenticate", authenticate)
        client.credentials(HTTP_AUTHORIZATION="Bearer admitted-service")
    monkeypatch.setattr("shared.authorization.port._provider_factory", Provider)
    monkeypatch.setattr("workspaces.services._policy_invitations._schedule_delivery", lambda _: None)
    account = services.create_account(kind="team", name="Synthetic")
    organization = Organization.objects.get(account_id=account.id)
    base = f"/api/v1/accounts/{account.uuid}/organizations/{organization.uuid}/workspaces/"
    created = client.post(base, {"name": "Additional"}, format="json")
    assert created.status_code == 201
    workspace_uuid = created.data["uuid"]
    base = f"/api/v1/workspaces/{workspace_uuid}/"
    account_base = f"/api/v1/account-workspaces/{workspace_uuid}/"
    for principal in (actor.principal, subject):
        assert (
            client.post(base + "memberships/", {"principal_uuid": str(principal.uuid)}, format="json").status_code
            == 204
        )
    assert client.get(account_base + "membership/").data["principal_uuid"] == str(actor.principal.uuid)
    assert (
        client.post(base + "owners/", {"principal_uuid": str(actor.principal.uuid)}, format="json").status_code == 204
    )
    assert (
        client.post(
            account_base + "ownership/transfer/",
            {"source_principal_uuid": str(actor.principal.uuid), "destination_principal_uuid": str(subject.uuid)},
            format="json",
        ).status_code
        == 204
    )
    invitation = client.post(account_base + "invitations/", {"email": "recipient@example.test"}, format="json")
    assert invitation.status_code == 201
    invitation_uuid = invitation.data["invitation_uuid"]
    assert client.get(account_base + "invitations/").data["count"] == 1
    for operation in ("resend", "revoke"):
        assert (
            client.post(account_base + f"invitations/{invitation_uuid}/{operation}/", {}, format="json").status_code
            == 200
        )
    assert (
        client.put(
            base + "quota-policy/", {"resource": "member_seats", "mode": "enforcing", "limit": 10}, format="json"
        ).status_code
        == 200
    )
    assert client.put(base + "egress-policy/", {"egress_policy": "none"}, format="json").status_code == 200
    assert client.post(base + "rename/", {"name": "Renamed"}, format="json").status_code == 200
    assert client.post(account_base + "memberships/leave/", {}, format="json").status_code == 204
    assert client.post(base + "archive/", {}, format="json").status_code == 200
    assert client.post(base + "restore/", {}, format="json").status_code == 200


def test_prepared_audit_contract_advertises_bearer_and_cookie_admission():
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(urlconf="config.account_admin_urls").get_schema(public=True)
    for path in ("/api/v1/audit/", "/api/v1/audit/{id}/"):
        assert {"ApiTokenAuth": []} in schema["paths"][path]["get"]["security"]
        assert {"cookieAuth": []} in schema["paths"][path]["get"]["security"]


@pytest.mark.urls("config.account_admin_urls")
def test_standalone_organization_operation_recovery_uses_persisted_ancestry(monkeypatch, settings):
    from django.contrib.auth import get_user_model

    from management.models import Principal
    from tests.workspaces.test_authorization_policy import FakeProvider

    user = get_user_model().objects.create_user(username="organization-recovery", password="test-pass")
    subject = Principal.objects.create(kind="service", name="Synthetic subject")
    account = services.create_account(kind="team", name="Synthetic account")
    organization = Organization.objects.get(account_id=account.id)
    provider = FakeProvider()
    provider.write_error = True
    settings.OPENFGA_MODEL_ID = provider.model_id
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    client = APIClient()
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    mutation = client.post(
        f"/api/v1/accounts/{account.uuid}/organizations/{organization.uuid}/authorization/direct-assignments/",
        {
            "subject_kind": "principal",
            "subject_uuid": str(subject.uuid),
            "action": "organization.read",
            "effect": "grant",
            "idempotency_key": str(uuid4()),
        },
        format="json",
    )
    assert mutation.status_code == 202 and mutation.data["state"] == "unresolved"
    operation_id = mutation.data["operation_id"]
    url = f"/api/v1/organizations/{organization.uuid}/authorization/operations/{operation_id}/"
    observed = client.get(url)
    assert observed.status_code == 200 and observed.data["state"] == "unresolved"
    provider.write_error = False
    reconciled = client.post(url, {}, format="json")
    assert reconciled.status_code == 200 and reconciled.data["state"] == "confirmed"
    other = Organization.objects.create(account=organization.account, name="Other organization")
    other_url = f"/api/v1/organizations/{other.uuid}/authorization/operations/{operation_id}/"
    assert client.get(other_url).status_code in {403, 404}
    provider.allowed = False
    assert client.get(url).status_code == 403


@pytest.mark.parametrize("kind", ["organization", "workspace"])
def test_descendant_only_administrator_can_discover_and_enter_its_exact_scope(monkeypatch, kind):
    account = services.create_account(kind="team", name="Routing parent")
    organization = Organization.objects.get(account_id=account.id)
    workspace = Workspace.objects.get(organization=organization)
    services.create_account(kind="team", name="Hidden parent")
    target = organization if kind == "organization" else workspace
    actor = _actor("service", actions=frozenset({f"{kind}.read"}))
    provider = Provider(allowed_targets=frozenset({target.uuid}))

    def authenticate(_self, request):
        request.credential_context = actor
        return None, actor

    monkeypatch.setattr("shared.api_tokens.authentication.ApiTokenAuthentication.authenticate", authenticate)
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer admitted-service")
    scopes = client.get("/api/v1/administer/scopes/", {"kind": kind, "limit": 1})
    assert scopes.status_code == 200
    assert scopes.data["count"] == 1
    assert scopes.data["results"][0]["uuid"] == str(target.uuid)
    assert scopes.data["results"][0]["account_uuid"] == str(account.uuid)
    assert client.get("/api/v1/accounts/").data["count"] == 0
    assert client.get("/api/v1/bootstrap/").data["permissions"]["can_administer_accounts"]
    context = client.get("/api/v1/accounts/context/")
    assert context.data["has_descendant_scopes"]
    assert provider.requests and all(request.action == f"{kind}.read" for request in provider.requests)


def test_lease_command_rejects_an_advertised_form_payload(django_user_model):
    user = django_user_model.objects.create_superuser(
        username="json-lease-administrator", password="synthetic-test-password"
    )
    client = APIClient()
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    response = client.put(
        "/api/v1/administer/mission-control/lease-policy/tenant/",
        "expected_revision=0&initial_days=1&maximum_days=3",
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 415
