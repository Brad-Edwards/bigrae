"""Coordinated S8 cutover state and conversion tests."""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from workspaces.models import Account, IdentityCutoverState, Organization, Workspace

pytestmark = pytest.mark.django_db


def test_cutover_state_is_singleton_and_activated_state_is_fully_bound() -> None:
    IdentityCutoverState.objects.create(
        state=IdentityCutoverState.State.PLANNED,
        plan_digest="a" * 64,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        IdentityCutoverState.objects.create(
            key=2,
            state=IdentityCutoverState.State.PLANNED,
            plan_digest="b" * 64,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        IdentityCutoverState.objects.filter(key=1).update(
            state=IdentityCutoverState.State.ACTIVATED,
            tuple_digest="",
            provider_store_id="",
            model_id="",
        )

    IdentityCutoverState.objects.filter(key=1).update(
        state=IdentityCutoverState.State.ACTIVATED,
        tuple_digest="c" * 64,
        provider_store_id="01J00000000000000000000001",
        model_id="01J00000000000000000000000",
        bootstrap_principal_uuid="00000000-0000-0000-0000-000000000001",
        bootstrap_verified_at=timezone.now(),
        admission_invalidated_at=timezone.now(),
    )
    assert IdentityCutoverState.objects.get().state == IdentityCutoverState.State.ACTIVATED


def test_hierarchy_verification_rejects_default_workspace_drift() -> None:
    from workspaces.services import IdentityCutoverConflict, verify_identity_cutover_hierarchy

    account = Account.objects.create(kind=Account.Kind.TEAM, name="Shared account")
    organization = Organization.objects.create(account=account, name="Default organization", is_default=True)
    workspace = Workspace.objects.create(organization=organization, name="Default workspace", is_default=True)
    plan = {
        "individuals": [],
        "organizations": [
            {
                "organization_id": organization.pk,
                "default_workspace_id": workspace.pk,
            }
        ],
    }
    verify_identity_cutover_hierarchy(plan)

    Workspace.objects.filter(pk=workspace.pk).update(is_default=False)

    with pytest.raises(IdentityCutoverConflict, match="default workspace"):
        verify_identity_cutover_hierarchy(plan)


@pytest.mark.parametrize(
    "defect", ["missing_organization", "missing_workspace", "archived_workspace", "individual_organization"]
)
def test_hierarchy_verification_checks_prepared_accounts_outside_the_conversion_plan(defect) -> None:
    from uuid import uuid4

    from workspaces.services import IdentityCutoverConflict, verify_identity_cutover_hierarchy

    account = Account.objects.create(
        kind=Account.Kind.INDIVIDUAL if defect == "individual_organization" else Account.Kind.TEAM,
        individual_principal_uuid=uuid4() if defect == "individual_organization" else None,
        name="Prepared account",
    )
    if defect != "missing_organization":
        organization = Organization.objects.create(account=account, name="Prepared organization", is_default=True)
        if defect == "archived_workspace":
            Workspace.objects.create(
                organization=organization, name="Archived default", is_default=True, archived_at=timezone.now()
            )
    with pytest.raises(IdentityCutoverConflict):
        verify_identity_cutover_hierarchy({"individuals": [], "organizations": []})


def test_hierarchy_verification_accepts_complete_prepared_hierarchy() -> None:
    from workspaces.services import verify_identity_cutover_hierarchy

    account = Account.objects.create(kind=Account.Kind.ENTERPRISE, name="Prepared account")
    for index in range(2):
        organization = Organization.objects.create(account=account, name=f"Organization {index}", is_default=index == 0)
        Workspace.objects.create(organization=organization, name="Active default", is_default=True)
    verify_identity_cutover_hierarchy({"individuals": [], "organizations": []})


@pytest.mark.parametrize("kind", ["organization", "workspace"])
def test_hierarchy_verification_rejects_omitted_user_keyed_membership(kind):
    from django.contrib.auth import get_user_model

    from workspaces.models import OrganizationMembership, WorkspaceMembership
    from workspaces.services import IdentityCutoverConflict, verify_identity_cutover_hierarchy

    user = get_user_model().objects.create_user(username="unconverted-membership")
    account = Account.objects.create(kind="team", name="Prepared account")
    organization = Organization.objects.create(account=account, name="Prepared organization", is_default=True)
    workspace = Workspace.objects.create(organization=organization, name="Prepared workspace", is_default=True)
    if kind == "organization":
        OrganizationMembership.objects.create(organization=organization, user=user, role="admin")
    else:
        WorkspaceMembership.objects.create(workspace=workspace, user=user, role="member")
    with pytest.raises(IdentityCutoverConflict, match="membership disposition"):
        verify_identity_cutover_hierarchy({"individuals": [], "organizations": [], "memberships": []})


def test_unsupported_workspace_admin_is_rejected_before_sql_or_policy_changes():
    from django.contrib.auth import get_user_model

    from management.services import principal_for_user
    from workspaces.models import WorkspaceMembership, WorkspaceOwnership
    from workspaces.services import (
        IdentityCutoverConflict,
        apply_identity_cutover_sql,
        identity_cutover_relationships,
    )

    user = get_user_model().objects.create_user(username="legacy-admin-disposition")
    principal = principal_for_user(user)
    account = Account.objects.create(kind="team", name="Prepared account")
    organization = Organization.objects.create(account=account, name="Prepared organization", is_default=True)
    workspace = Workspace.objects.create(organization=organization, name="Prepared workspace", is_default=True)
    membership = WorkspaceMembership.objects.create(workspace=workspace, user=user, role="admin")
    plan = {
        "individuals": [],
        "organizations": [],
        "memberships": [
            {
                "scope_kind": "workspace",
                "scope_id": workspace.pk,
                "user_id": user.pk,
                "principal_uuid": principal.uuid,
                "role": "admin",
            }
        ],
    }
    for operation in (apply_identity_cutover_sql, identity_cutover_relationships):
        with pytest.raises(IdentityCutoverConflict, match="policy disposition"):
            operation(plan)
    membership.refresh_from_db()
    assert membership.user_id == user.pk
    assert membership.principal_uuid is None
    assert membership.role == "admin"
    assert not WorkspaceOwnership.objects.filter(workspace=workspace).exists()


def test_resource_mapping_includes_preexisting_account_bindings_and_rejects_invalid_hierarchy():
    from workspaces.services import IdentityCutoverConflict, identity_cutover_resource_mappings

    account = Account.objects.create(kind="team", name="Prepared account")
    organization = Organization.objects.create(account=account, name="Prepared organization", is_default=True)
    workspace = Workspace.objects.create(organization=organization, name="Prepared workspace", is_default=True)
    plan = {"individuals": [], "organizations": [], "memberships": []}
    assert identity_cutover_resource_mappings(plan) == {workspace.pk: (account.pk, organization.pk, workspace.pk)}
    workspace.is_default = False
    workspace.save(update_fields=["is_default"])
    with pytest.raises(IdentityCutoverConflict, match="default workspace"):
        identity_cutover_resource_mappings(plan)
