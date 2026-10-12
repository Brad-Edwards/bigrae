"""Policy offboarding preserves source/destination eligibility and all owner projections."""

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model

from cms import services
from cms.models import RangeInstance
from cms.models import Request as CmsRequest
from engine.models import Range as EngineRange
from engine.models import Request as EngineRequest
from management import services as identities
from shared.authorization import APPLICATION_ADMINISTRATOR_ACTIONS
from shared.enums import RangeSource, RequestType, ResourceStatus
from shared.models import AuditLog
from tests.workspaces.test_account_policy_admin import Provider, _actor
from workspaces import services as workspaces
from workspaces.models import Organization, Workspace, WorkspaceOwnership

pytestmark = pytest.mark.django_db


@pytest.fixture
def owned_resources():
    source = get_user_model().objects.create_user(username=f"source-{uuid4()}")
    destination = get_user_model().objects.create_user(username=f"destination-{uuid4()}")
    source_ref, destination_ref = identities.principal_for_user(source), identities.principal_for_user(destination)
    account = workspaces.create_account(kind="team", name="Customer")
    organization = Organization.objects.get(account_id=account.id)
    workspace = Workspace.objects.get(organization=organization)
    actor = _actor("service", actions=APPLICATION_ADMINISTRATOR_ACTIONS | frozenset({"range.manage"}))
    for principal in (source_ref, destination_ref):
        workspaces.admin_add_scope_member(actor, Provider(), "workspace", workspace.uuid, principal)
    WorkspaceOwnership.objects.create(workspace=workspace, principal_uuid=source_ref.uuid)
    request_id = uuid4()
    scope = {
        "scope_kind": "account",
        "account_id": account.id,
        "organization_id": organization.pk,
        "workspace_id": workspace.pk,
    }
    cms_request = CmsRequest.objects.create(
        request_id=request_id, request_type=RequestType.RANGE.value, user=source, **scope
    )
    engine_request = EngineRequest.objects.create(
        request_id=request_id, request_type=RequestType.RANGE.value, user=source
    )
    engine_range = EngineRange.objects.create(
        user=source,
        cms_user_id=source.pk,
        request=engine_request,
        status=EngineRange.Status.READY,
        subnet_index=EngineRange.allocate_subnet_index(),
        **scope,
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        user_id=source.pk,
        scenario_id="synthetic",
        range_source=RangeSource.MISSION_CONTROL.value,
        status=ResourceStatus.READY.value,
        **scope,
    )
    return actor, source, destination, workspace, cms_request, engine_range, engine_request, instance


def test_service_offboards_inactive_source_without_membership_or_policy_side_effects(owned_resources):
    actor, source, destination, workspace, cms_request, engine_range, engine_request, instance = owned_resources
    source.is_active = False
    source.save(update_fields=["is_active"])
    summary = services.policy_transfer_user_ownership(
        actor, Provider(), source, destination, kinds=("workspaces", "ranges")
    )
    assert summary.workspaces_transferred == 1 and summary.ranges_reassigned == 1
    for row in (workspace, cms_request, engine_range, engine_request, instance):
        row.refresh_from_db()
    assert workspace.ownerships.get().principal_uuid == identities.principal_for_user(destination).uuid
    assert cms_request.user_id == engine_range.user_id == engine_request.user_id == instance.user_id == destination.pk
    assert AuditLog.objects.filter(
        entity_type="range", actor_principal_uuid=actor.principal.uuid, action="update"
    ).exists()
    assert workspaces.admin_list_scope_members(actor, Provider(), "workspace", workspace.uuid).count == 2


def test_transfer_denial_and_projection_drift_preserve_ownership(owned_resources):
    actor, source, destination, _workspace, cms_request, engine_range, _engine_request, instance = owned_resources
    with pytest.raises(PermissionError):
        services.policy_transfer_user_ownership(actor, Provider(allowed=False), source, destination, kinds=("ranges",))
    engine_range.user = destination
    engine_range.save(update_fields=["user"])
    summary = services.policy_transfer_user_ownership(actor, Provider(), source, destination, kinds=("ranges",))
    assert summary.ranges_blocked == 1 and summary.ranges_reassigned == 0
    cms_request.refresh_from_db()
    instance.refresh_from_db()
    assert cms_request.user_id == instance.user_id == source.pk


def test_offboarding_does_not_fabricate_destination_membership(owned_resources):
    actor, source, destination, workspace, *_ = owned_resources
    principal = identities.principal_for_user(destination)
    workspaces.admin_remove_scope_member(actor, Provider(), "workspace", workspace.uuid, principal.uuid)
    summary = services.policy_transfer_user_ownership(
        actor, Provider(), source, destination, kinds=("workspaces", "ranges")
    )
    assert summary.workspaces_blocked_no_membership == 1 and summary.ranges_blocked == 1
    workspace.refresh_from_db()
    assert workspace.ownerships.get().principal_uuid == identities.principal_for_user(source).uuid


@pytest.mark.parametrize("policy", [True, False])
def test_offboarding_does_not_adopt_an_intervening_owner(owned_resources, monkeypatch, policy):
    from cms.services import _policy_offboarding, _user_offboarding
    from workspaces.models import WorkspaceMembership

    actor, source, destination, workspace, cms_request, engine_range, engine_request, instance = owned_resources
    intervening = get_user_model().objects.create_user(username=f"intervening-{uuid4()}")
    if not policy:
        WorkspaceMembership.objects.create(workspace=workspace, user=destination, role="member")
    module = _policy_offboarding if policy else _user_offboarding
    name = "policy_reassign_range_owner" if policy else "reassign_range_owner"
    original = getattr(module, name)

    def change_owner():
        for row in (instance, cms_request, engine_request):
            row.user_id = intervening.pk
            row.save(update_fields=["user_id"])
        engine_range.user_id = engine_range.cms_user_id = intervening.pk
        engine_range.save(update_fields=["user_id", "cms_user_id"])

    if policy:

        def race(*args, **kwargs):
            change_owner()
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, race)
    else:
        from types import SimpleNamespace

        original_filter = RangeInstance.objects.filter

        def enumerate_then_race(*args, **kwargs):
            rows = original_filter(*args, **kwargs)
            if kwargs.get("user_id") != source.pk:
                return rows
            ids = list(rows.values_list("pk", flat=True))
            change_owner()
            return SimpleNamespace(values_list=lambda *_args, **_kwargs: ids)

        monkeypatch.setattr(RangeInstance.objects, "filter", enumerate_then_race)
    if policy:
        result = services.policy_transfer_user_ownership(actor, Provider(), source, destination, kinds=("ranges",))
        assert result.ranges_blocked == 1 and result.ranges_reassigned == 0
    else:
        assert module._transfer_ranges(source, destination, module.OffboardingAuditContext("user", source.pk)) == (0, 1)
    for row in (instance, cms_request, engine_range, engine_request):
        row.refresh_from_db()
        assert row.user_id == intervening.pk


def test_active_range_collision_preserves_prior_workspace_transfer(owned_resources):
    actor, source, destination, workspace, *_rest, instance = owned_resources
    RangeInstance.objects.create(
        user_id=destination.pk,
        range_source=instance.range_source,
        status=instance.status,
        scenario_id="synthetic-destination",
        scope_kind=instance.scope_kind,
        account_id=instance.account_id,
        organization_id=instance.organization_id,
        workspace_id=instance.workspace_id,
    )
    result = services.policy_transfer_user_ownership(
        actor, Provider(), source, destination, kinds=("workspaces", "ranges")
    )
    assert result.workspaces_transferred == 1 and result.ranges_blocked == 1 and result.ranges_reassigned == 0
    instance.refresh_from_db()
    assert instance.user_id == source.pk
    assert workspace.ownerships.get().principal_uuid == identities.principal_for_user(destination).uuid
