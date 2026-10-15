"""S6 catalog coverage keeps service administration and ceilings coherent."""

from uuid import uuid4

import pytest

from shared.api_tokens.scopes import AUTHORIZATION_ACTION_SCOPES, credential_ceiling
from shared.authorization import (
    APPLICATION_ADMINISTRATOR_ACTIONS,
    AuthorizationRequest,
    CredentialCeiling,
    TargetRef,
    action_definition,
)
from shared.authorization.catalog import AuthorizationContractError
from shared.identity_scope import PrincipalRef, ResourceScope


@pytest.mark.parametrize("parent", ["account", "organization", "workspace"])
@pytest.mark.parametrize(
    "operation",
    [
        "read_content",
        "manage_content",
        "manage_sources",
        "manage_credentials",
        "manage_adapters",
        "prepare_content",
        "publish_content",
        "launch_range",
        "read_devices",
        "manage_devices",
        "access_devices",
    ],
)
def test_content_device_and_launch_actions_have_exact_scopes_and_administrator_coverage(parent, operation):
    action = f"{parent}.{operation}"
    assert action_definition(action).target_type == parent
    assert action in APPLICATION_ADMINISTRATOR_ACTIONS
    target = TargetRef(parent, uuid4())
    ceiling = credential_ceiling([AUTHORIZATION_ACTION_SCOPES[action]], target=target)
    assert ceiling.permits(action, target)
    assert not ceiling.permits(action, TargetRef(parent, uuid4()))


@pytest.mark.parametrize(
    "action",
    [
        "account.read",
        "organization.read",
        "workspace.read",
        "workspace.launch_range",
        "event.read",
        "range.read",
        "range.manage",
        "range.access",
    ],
)
def test_full_administration_covers_valid_resource_operations_without_participation(action):
    assert action in APPLICATION_ADMINISTRATOR_ACTIONS
    assert "event.participate" not in APPLICATION_ADMINISTRATOR_ACTIONS
    assert "installation.use_personal_tokens" not in APPLICATION_ADMINISTRATOR_ACTIONS


def test_account_content_authority_cannot_be_relabelled_as_operator_or_sibling_scope():
    principal = PrincipalRef(uuid4(), "service")
    account = uuid4()
    target = TargetRef("account", account)
    ceiling = CredentialCeiling(frozenset({"account.manage_content"}), target)
    request = AuthorizationRequest(
        principal, "account.manage_content", target, ResourceScope("account", account), ceiling
    )
    assert request.target == target
    with pytest.raises(AuthorizationContractError):
        AuthorizationRequest(principal, "account.manage_content", TargetRef("account", uuid4()), request.scope, ceiling)
    with pytest.raises(AuthorizationContractError):
        AuthorizationRequest(
            principal, "installation.manage_adapters", TargetRef("installation"), ResourceScope("installation"), ceiling
        )
