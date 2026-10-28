"""Prepared account administration against the released OpenFGA server."""

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from openfga_sdk.client.models import ClientTuple

from management.models import Principal
from shared.authorization import (
    AdministrativeRoleChange,
    CredentialCeiling,
    PolicyEffect,
    RelationshipSubject,
    TargetRef,
)
from shared.identity_scope import PrincipalRef
from tests.integration.authorization.test_openfga_released_server import _write_fixture_tuple
from tests.integration.authorization.test_openfga_released_server import provider as provider

pytestmark = [pytest.mark.openfga, pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("kind", ["human", "service"])
def test_prepared_administrator_discovers_hierarchy_and_edits_without_membership(provider, kind):
    from management import services as identities
    from shared.authorization.catalog import APPLICATION_ADMINISTRATOR_ACTIONS
    from shared.credentials import CredentialContext
    from workspaces import services as tenancy
    from workspaces.models import Organization, Workspace

    if kind == "human":
        user = get_user_model().objects.create_user(username=f"admin-{uuid4()}")
        principal = identities.principal_for_user(user)
    else:
        row = Principal.objects.create(kind="service", name="Synthetic administrator")
        principal = PrincipalRef(row.uuid, "service")
    actor = CredentialContext(
        principal,
        "session" if kind == "human" else "service",
        uuid4(),
        CredentialCeiling(APPLICATION_ADMINISTRATOR_ACTIONS),
        frozenset(),
    )
    account = tenancy.create_account(kind="team", name="Synthetic team")
    organization = Organization.objects.get(account_id=account.id)
    workspace = Workspace.objects.get(organization=organization)
    for user, relation, target in (
        ("installation:root", "installation", f"account:{account.uuid}"),
        (f"account:{account.uuid}", "account", f"organization:{organization.uuid}"),
        (f"organization:{organization.uuid}", "organization", f"workspace:{workspace.uuid}"),
    ):
        _write_fixture_tuple(provider, ClientTuple(user, relation, target))
    provider.write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", principal.uuid),
            "application_administrator",
            TargetRef("installation"),
            PolicyEffect.GRANT,
        )
    )
    assert tenancy.admin_get_account(actor, provider, account.uuid).uuid == account.uuid
    assert tenancy.admin_list_organizations(actor, provider, account.uuid).results[0].uuid == organization.uuid
    assert (
        tenancy.admin_list_workspaces(actor, provider, account.uuid, organization.uuid).results[0].uuid
        == workspace.uuid
    )
    tenancy.admin_update_organization_profile(
        actor, provider, account.uuid, organization.uuid, {"description": "Updated"}
    )
    assert tenancy.admin_workspace_quota_usage(actor, provider, workspace.uuid).workspace_uuid == workspace.uuid
    assert not actor.ceiling.permits("installation.use_personal_tokens", TargetRef("installation"))
    limited = CredentialContext(
        principal,
        actor.kind,
        uuid4(),
        CredentialCeiling(frozenset({"account.read"}), target=TargetRef("account", account.uuid)),
        frozenset(),
    )
    assert tenancy.admin_get_account(limited, provider, account.uuid).uuid == account.uuid
    with pytest.raises(tenancy.AccountScopeError):
        tenancy.admin_update_organization_profile(
            limited, provider, account.uuid, organization.uuid, {"description": "Denied"}
        )
