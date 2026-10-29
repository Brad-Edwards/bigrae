"""Workspace admin inspection stays scoped and fails closed at its boundaries."""

import logging

import pytest
from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from shared.admin_policy import PolicyAdminSite, PolicyReadOnlyAdmin
from tests.workspaces.test_account_policy_admin import Provider, _actor
from workspaces import services
from workspaces.models import Workspace, WorkspaceQuotaPolicy
from workspaces.policy_admin import PolicyWorkspaceQuotaAdmin

pytestmark = pytest.mark.django_db


def _request():
    actor = _actor(actions=frozenset({"workspace.manage_quota"}))
    request = RequestFactory().get("/admin/")
    request.user = get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid)
    request.credential_context = actor
    return request


def test_workspace_admin_filters_before_counting_and_denies_hidden_objects(monkeypatch):
    request = _request()
    visible = services.create_account(kind="team", name="Visible")
    hidden = services.create_account(kind="team", name="Hidden")
    quotas = [
        WorkspaceQuotaPolicy.objects.create(
            workspace=Workspace.objects.get(organization__account_id=account.id, is_default=True),
            resource="concurrent_ranges",
            limit=3,
            mode="enforcing",
        )
        for account in (visible, hidden)
    ]
    provider = Provider(allowed_targets=frozenset({quotas[0].workspace.uuid}))
    monkeypatch.setattr("workspaces.policy_admin.configured_authorization_provider", lambda: provider)
    inspection = PolicyWorkspaceQuotaAdmin(WorkspaceQuotaPolicy, AdminSite())

    assert inspection.has_view_permission(request)
    assert inspection.has_view_permission(request, quotas[0])
    assert not inspection.has_view_permission(request, quotas[1])
    assert list(inspection.get_queryset(request)) == [quotas[0]]
    assert {check.action for check in provider.requests} == {"workspace.manage_quota"}
    assert not inspection.has_change_permission(request, quotas[0])


@pytest.mark.parametrize("object_check", [False, True])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_workspace_admin_denies_failed_credential_resolution(error_type, object_check, monkeypatch, caplog):
    def unavailable(request):
        raise error_type("Synthetic credential resolver outage")

    monkeypatch.setattr("shared.admin_policy.authenticated_credential", unavailable)
    inspection = PolicyWorkspaceQuotaAdmin(WorkspaceQuotaPolicy, AdminSite())
    request = RequestFactory().get("/admin/")
    with caplog.at_level(logging.ERROR, logger="workspaces.policy_admin"):
        allowed = (
            inspection.has_view_permission(request, WorkspaceQuotaPolicy())
            if object_check
            else inspection.has_module_permission(request)
        )
    assert not allowed
    errors = [record for record in caplog.records if record.name == "workspaces.policy_admin"]
    assert bool(errors) == (error_type is RuntimeError)
    if errors:
        assert "access denied" in errors[0].getMessage()


@pytest.mark.parametrize("boundary", ["site", "view"])
def test_admin_denies_an_unexpected_credential_resolution_failure(boundary, monkeypatch, caplog):
    def unavailable(request):
        raise RuntimeError("Synthetic credential resolver outage")

    monkeypatch.setattr("shared.admin_policy.authenticated_credential", unavailable)
    request = RequestFactory().get("/admin/")
    with caplog.at_level(logging.ERROR, logger="shared.admin_policy"):
        if boundary == "site":
            assert not PolicyAdminSite().has_permission(request)
        else:
            assert not PolicyReadOnlyAdmin(WorkspaceQuotaPolicy, AdminSite()).has_view_permission(request)
    assert f"policy admin {boundary} check failed; access denied" in caplog.text
