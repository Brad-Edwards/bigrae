"""Prepared lease administration accepts independent policy service principals."""

import pytest
from django.contrib.auth.models import Group

from cms import services
from shared.mission_control_lease import MissionControlLeasePolicy
from tests.workspaces.test_account_policy_admin import Provider, _actor

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["human", "service"])
def test_policy_administrator_reads_replaces_and_resets_lease_policy(monkeypatch, kind):
    actor = _actor(kind, actions=frozenset({"installation.manage_lease_policy"}))
    monkeypatch.setattr("cms.services._range_lease_policy_support.configured_authorization_provider", Provider)
    policy = MissionControlLeasePolicy(initial_days=1, extension_days=1, maximum_days=3, extensions_enabled=True)
    audit = services.LeasePolicyAuditContext(
        actor_type="principal", actor_id=None, actor_principal_uuid=actor.principal.uuid
    )
    before = services.get_mission_control_lease_settings(actor)
    services.replace_tenant_lease_policy(actor, policy, expected_revision=before.tenant_revision, audit=audit)
    current = services.get_mission_control_lease_settings(actor)
    assert current.tenant_override.policy == policy
    group = Group.objects.create(name="Lease group")
    services.replace_group_lease_policy(actor, group.pk, policy, expected_revision=0, audit=audit)
    current = services.get_mission_control_lease_settings(actor)
    group_state = next(item for item in current.groups if item.group_id == group.pk)
    services.reset_group_lease_policy(actor, group.pk, expected_revision=group_state.revision, audit=audit)
    services.reset_tenant_lease_policy(actor, expected_revision=current.tenant_revision, audit=audit)
    assert services.get_mission_control_lease_settings(actor).tenant_override is None


def test_policy_lease_denial_and_ceiling_preserve_settings(monkeypatch):
    actor = _actor("service", actions=frozenset({"installation.manage_lease_policy"}))
    monkeypatch.setattr(
        "cms.services._range_lease_policy_support.configured_authorization_provider", lambda: Provider(allowed=False)
    )
    with pytest.raises(services.MissionControlLeasePolicyAdminError):
        services.get_mission_control_lease_settings(actor)
    monkeypatch.setattr("cms.services._range_lease_policy_support.configured_authorization_provider", Provider)
    with pytest.raises(services.MissionControlLeasePolicyAdminError):
        services.get_mission_control_lease_settings(
            _actor("service", actions=frozenset({"installation.manage_principals"}))
        )


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("during_policy", [False, True])
def test_revoked_native_lease_administrator_cannot_write_policy(monkeypatch, kind, during_policy):
    from cms.models import MissionControlTenantLeasePolicy
    from management.models import PrincipalSession, ServiceCredentialAdmission

    actor = _actor(kind, actions=frozenset({"installation.manage_lease_policy"}))

    def revoke():
        if kind == "human":
            PrincipalSession.objects.filter(credential_uuid=actor.credential_uuid).delete()
        else:
            ServiceCredentialAdmission.objects.filter(uuid=actor.credential_uuid).update(is_active=False)

    class RevokingProvider(Provider):
        def check(self, request):
            decision = super().check(request)
            if during_policy:
                revoke()
            return decision

    monkeypatch.setattr("cms.services._range_lease_policy_support.configured_authorization_provider", RevokingProvider)
    if not during_policy:
        revoke()
    with pytest.raises(services.MissionControlLeasePolicyAdminError):
        services.replace_tenant_lease_policy(
            actor,
            MissionControlLeasePolicy(initial_days=1, extension_days=1, maximum_days=3, extensions_enabled=True),
            expected_revision=0,
            audit=services.LeasePolicyAuditContext(
                actor_type="principal", actor_id=None, actor_principal_uuid=actor.principal.uuid
            ),
        )
    assert not MissionControlTenantLeasePolicy.objects.exists()


def test_synchronous_superuser_requires_explicit_lease_policy(monkeypatch):
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_superuser(username="no-lease-grant", password="test-pass")
    monkeypatch.setattr(
        "cms.services._range_lease_policy_support.configured_authorization_provider", lambda: Provider(allowed=False)
    )
    with pytest.raises(services.MissionControlLeasePolicyAdminError):
        services.get_mission_control_lease_settings(user)


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("encoding", ["form", "multipart"])
@pytest.mark.parametrize("scope", ["tenant", "group"])
def test_published_lease_form_formats_replace_and_reset_native_policy(monkeypatch, kind, encoding, scope):
    from urllib.parse import urlencode

    from rest_framework.test import APIRequestFactory, force_authenticate

    from config import api_administer_leases as api

    actor = _actor(kind, actions=frozenset({"installation.manage_lease_policy"}))
    monkeypatch.setattr("cms.services._range_lease_policy_support.configured_authorization_provider", Provider)
    group = Group.objects.create(name="Form lease group")
    kwargs = {"group_id": group.pk} if scope == "group" else {}
    replace_view = (
        api.PolicyMissionControlGroupLeasePolicyView
        if scope == "group"
        else api.PolicyMissionControlTenantLeasePolicyView
    )
    reset_view = (
        api.PolicyMissionControlGroupLeasePolicyResetView
        if scope == "group"
        else api.PolicyMissionControlTenantLeasePolicyResetView
    )

    def call(view, method, data):
        factory = APIRequestFactory()
        if encoding == "multipart":
            request = getattr(factory, method)("/lease-policy/", data, format="multipart")
        else:
            request = factory.generic(
                method.upper(), "/lease-policy/", urlencode(data), content_type="application/x-www-form-urlencoded"
            )
        request.credential_context = actor
        force_authenticate(request, token=actor)
        return view.as_view()(request, **kwargs)

    response = call(
        replace_view,
        "put",
        {
            "expected_revision": "0",
            "initial_days": "1",
            "extension_days": "1",
            "maximum_days": "3",
            "extensions_enabled": "false",
        },
    )
    assert response.status_code == 200, response.data
    state = (
        next(item for item in response.data["groups"] if item["id"] == group.pk) if scope == "group" else response.data
    )
    override = state["override"] if scope == "group" else state["tenant_override"]
    assert override["policy"] == {
        "initial_days": 1,
        "extension_days": 1,
        "maximum_days": 3,
        "extensions_enabled": False,
    }
    response = call(reset_view, "post", {"expected_revision": str(override["revision"])})
    assert response.status_code == 200, response.data
    state = (
        next(item for item in response.data["groups"] if item["id"] == group.pk) if scope == "group" else response.data
    )
    assert (state["override"] if scope == "group" else state["tenant_override"]) is None


@pytest.mark.parametrize(
    "defect", ["missing_boolean", "duplicate_revision", "unknown_field", "invalid_integer", "invalid_boolean"]
)
def test_lease_form_contract_rejects_ambiguous_or_invalid_fields(defect):
    from django.http import QueryDict

    from config.api_administer_leases import ReplaceLeasePolicySerializer

    data = QueryDict(
        "expected_revision=0&initial_days=1&extension_days=1&maximum_days=3&extensions_enabled=false", mutable=True
    )
    if defect == "missing_boolean":
        data.pop("extensions_enabled")
    elif defect == "duplicate_revision":
        data.appendlist("expected_revision", "1")
    elif defect == "unknown_field":
        data["unexpected"] = "value"
    elif defect == "invalid_integer":
        data["initial_days"] = "false"
    else:
        data["extensions_enabled"] = "maybe"
    assert not ReplaceLeasePolicySerializer(data=data).is_valid()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", "0"),
        ("initial_days", True),
        ("extension_days", 1.5),
        ("extensions_enabled", "false"),
        ("extensions_enabled", 0),
    ],
)
def test_native_json_lease_scalars_remain_strict(field, value):
    from config.api_administer_leases import ReplaceLeasePolicySerializer

    data = {
        "expected_revision": 0,
        "initial_days": 1,
        "extension_days": 1,
        "maximum_days": 3,
        "extensions_enabled": False,
    }
    data[field] = value
    assert not ReplaceLeasePolicySerializer(data=data).is_valid()
