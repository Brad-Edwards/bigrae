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
