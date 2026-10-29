"""Policy recovery and mutation fences reject changed authority bindings."""

from dataclasses import replace
from uuid import UUID

import pytest
from django.contrib.auth import get_user_model

from shared.authorization import (
    ACTION_CATALOG,
    AdministrativeRoleChange,
    CredentialCeiling,
    PolicyEffect,
    RelationshipSubject,
    TargetRef,
)
from shared.identity_scope import PrincipalRef, ResourceScope
from tests.workspaces.test_authorization_policy import (
    ACTOR_ID,
    MODEL_ID,
    SUBJECT_ID,
    FakeProvider,
    _authorization_principals,
    _request,
)
from workspaces.models import Account, AuthorizationOperation, Organization, Workspace
from workspaces.services import (
    AuthorizationMutationConflict,
    NativeRelationshipMutationRequest,
    apply_native_relationship_mutation,
    apply_policy_mutation,
    reconcile_policy_mutation,
)

__all__ = ["_authorization_principals"]
pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("changed_field", ["model_id", "store_id"])
def test_recovery_refuses_changed_provider_binding(changed_field) -> None:
    account = Account.objects.create(kind=Account.Kind.TEAM, name="A")
    result = apply_policy_mutation(_request(account), FakeProvider(write_error=True))
    replacement = FakeProvider()
    setattr(replacement, changed_field, "01J00000000000000000000009")

    with pytest.raises(AuthorizationMutationConflict, match="binding changed"):
        reconcile_policy_mutation(result.operation_id, replacement)

    assert replacement.changes == []
    operation = AuthorizationOperation.objects.get(pk=result.operation_id)
    assert operation.state == AuthorizationOperation.State.UNRESOLVED
    assert operation.reconcile_lease_until is None


def test_mutation_rejects_model_different_from_evaluator_before_reservation() -> None:
    account = Account.objects.create(kind=Account.Kind.TEAM, name="A")
    provider = FakeProvider()

    with pytest.raises(AuthorizationMutationConflict, match="binding changed"):
        apply_policy_mutation(replace(_request(account), model_id="01J00000000000000000000009"), provider)

    assert provider.changes == []
    assert not AuthorizationOperation.objects.exists()


@pytest.mark.parametrize("target_type,action", [("event", "event.manage"), ("range", "range.manage")])
def test_leaf_mutation_rejects_unowned_target_even_when_evaluator_allows(target_type, action) -> None:
    account = Account.objects.create(kind=Account.Kind.TEAM, name="A")
    organization = Organization.objects.create(account=account, name="O")
    workspace = Workspace.objects.create(organization=organization, name="W")
    request = replace(
        _request(account),
        action=action,
        target=TargetRef(target_type, UUID("33333333-3333-3333-3333-333333333333")),
        scope=ResourceScope("account", account.uuid, organization.uuid, workspace.uuid),
        credential=CredentialCeiling(frozenset(item.code for item in ACTION_CATALOG)),
    )
    provider = FakeProvider()

    result = apply_policy_mutation(request, provider)

    assert result.state == AuthorizationOperation.State.DENIED
    assert provider.check_requests == []
    assert provider.changes == []


def test_disabled_human_actor_cannot_delegate_through_service_entrypoint() -> None:
    account = Account.objects.create(kind=Account.Kind.TEAM, name="A")
    get_user_model().objects.filter(identity_principal__uuid=ACTOR_ID).update(is_active=False)
    provider = FakeProvider()

    result = apply_policy_mutation(_request(account), provider)

    assert result.state == AuthorizationOperation.State.DENIED
    assert provider.check_requests == []
    assert provider.changes == []


@pytest.mark.parametrize("target_type", ["installation", "account", "organization"])
def test_broad_administrator_checks_each_descendant_with_its_own_scope(target_type) -> None:
    account = Account.objects.create(kind=Account.Kind.TEAM, name="A")
    organization = Organization.objects.create(account=account, name="O")
    workspace = Workspace.objects.create(organization=organization, name="W")
    target, scope = {
        "installation": (TargetRef("installation"), ResourceScope("installation")),
        "account": (TargetRef("account", account.uuid), ResourceScope("account", account.uuid)),
        "organization": (
            TargetRef("organization", organization.uuid),
            ResourceScope("account", account.uuid, organization.uuid),
        ),
    }[target_type]
    provider = FakeProvider()
    request = NativeRelationshipMutationRequest(
        actor=PrincipalRef(ACTOR_ID, "human"),
        credential=CredentialCeiling(frozenset(item.code for item in ACTION_CATALOG)),
        change=AdministrativeRoleChange(
            RelationshipSubject("principal", SUBJECT_ID),
            "application_administrator" if target_type == "installation" else f"{target_type}_administrator",
            target,
            PolicyEffect.GRANT,
        ),
        scope=scope,
        idempotency_key=f"broad-{target_type}",
        model_id=MODEL_ID,
    )

    result = apply_native_relationship_mutation(request, provider)

    assert result.state == AuthorizationOperation.State.CONFIRMED
    workspace_checks = [
        item for item in provider.check_requests if item.target == TargetRef("workspace", workspace.uuid)
    ]
    assert workspace_checks
    assert all(
        item.scope == ResourceScope("account", account.uuid, organization.uuid, workspace.uuid)
        for item in workspace_checks
    )
