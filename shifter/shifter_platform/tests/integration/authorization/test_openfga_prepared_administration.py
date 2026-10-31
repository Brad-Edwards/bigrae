"""Prepared account administration against the released OpenFGA server."""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from openfga_sdk.client.models import ClientTuple

from shared.authorization import (
    AdministrativeRoleChange,
    CredentialCeiling,
    PolicyEffect,
    RelationshipSubject,
    TargetRef,
)
from tests.integration.authorization.test_openfga_released_server import _write_fixture_tuple
from tests.integration.authorization.test_openfga_released_server import provider as provider

pytestmark = [pytest.mark.openfga, pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("kind", ["human", "service"])
def test_prepared_administrator_discovers_hierarchy_and_edits_without_membership(
    provider, kind, live_session_credential, monkeypatch, settings
):
    from management import services as identities
    from shared.authorization import port
    from shared.authorization.catalog import APPLICATION_ADMINISTRATOR_ACTIONS
    from workspaces import services as tenancy
    from workspaces.models import Organization, Workspace

    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    user = get_user_model().objects.create_user(username=f"admin-{uuid4()}")
    actor = replace(live_session_credential(user), ceiling=CredentialCeiling(APPLICATION_ADMINISTRATOR_ACTIONS))
    principal = actor.principal
    provider.write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", principal.uuid),
            "application_administrator",
            TargetRef("installation"),
            PolicyEffect.GRANT,
        )
    )
    if kind == "service":
        from management.service_credentials import create_service_credential

        # The synthetic subject is a Google numeric service-account ID.
        settings.CLOUD_PROVIDER = "gcp"
        admission = create_service_credential(
            actor,
            name="Synthetic administrator",
            subject="123456789012345678901",
            scopes=[f"authorization:{action}" for action in sorted(APPLICATION_ADMINISTRATOR_ACTIONS)],
        )
        actor = identities.resolve_service_credential_reference(
            admission["credential_uuid"], audience=settings.GCP_SERVICE_TOKEN_AUDIENCE
        )
        principal = actor.principal
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
    limited = replace(
        actor, ceiling=CredentialCeiling(frozenset({"account.read"}), target=TargetRef("account", account.uuid))
    )
    assert tenancy.admin_get_account(limited, provider, account.uuid).uuid == account.uuid
    with pytest.raises(tenancy.AccountScopeError):
        tenancy.admin_update_organization_profile(
            limited, provider, account.uuid, organization.uuid, {"description": "Denied"}
        )


@pytest.mark.parametrize("operation", ["transfer", "assign", "rerole"])
def test_real_authority_plan_recovers_lost_ack_with_current_admin_and_inactive_subject(
    provider, live_session_credential, monkeypatch, operation
):
    """Exercise real SQL, live proofs and released generation-bound OpenFGA writes."""
    from datetime import timedelta

    from django.utils import timezone

    from ctf.exceptions import CTFStateError
    from ctf.models import CTFEvent, CTFEventAuthorityTransition
    from ctf.services.event import assign_event_staff, transfer_event_ownership
    from management.models import Principal
    from shared.authorization import AuthorizationProviderError, AuthorizationRequest, port
    from shared.identity_scope import ResourceScope
    from workspaces.models import Account, AuthorizationOperation

    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    owner = get_user_model().objects.create_user(username=f"owner-{uuid4()}")
    recipient = get_user_model().objects.create_user(username=f"recipient-{uuid4()}")
    administrator = get_user_model().objects.create_user(username=f"administrator-{uuid4()}")
    original = live_session_credential(owner)
    current = live_session_credential(administrator)
    account = Account.objects.create(
        kind="individual", name="Synthetic handoff account", individual_principal_uuid=original.principal.uuid
    )
    now = timezone.now()
    event = CTFEvent.objects.create(
        name="Synthetic retained handoff",
        created_by=owner,
        owner_principal_uuid=original.principal.uuid,
        scope_kind="account",
        account_id=account.pk,
        scenario_id="synthetic-handoff",
        event_start=now,
        event_end=now + timedelta(days=1),
    )
    for user, relation, target in (
        ("installation:root", "installation", f"account:{account.uuid}"),
        (f"account:{account.uuid}", "account", f"event:{event.pk}"),
    ):
        _write_fixture_tuple(provider, ClientTuple(user, relation, target))
    provider.write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", original.principal.uuid),
            "event_administrator",
            TargetRef("event", event.pk),
            PolicyEffect.GRANT,
        )
    )
    provider.write_relationships(
        AdministrativeRoleChange(
            RelationshipSubject("principal", current.principal.uuid),
            "application_administrator",
            TargetRef("installation"),
            PolicyEffect.GRANT,
        )
    )
    target_uuid = recipient.identity_principal.uuid
    if operation != "assign":
        assign_event_staff(event.pk, original, target_uuid, "co_organizer" if operation == "transfer" else "moderator")
    read = provider.read_relationships
    interrupted = False

    def lose_recipient_acknowledgement(change):
        nonlocal interrupted
        result = read(change)
        if (
            not interrupted
            and change.grant_tuple.user == f"principal:{target_uuid}"
            and (operation != "transfer" or change.grant_tuple.relation == "direct_administrator")
        ):
            interrupted = True
            raise AuthorizationProviderError("Synthetic acknowledgement loss after actual commit")
        return result

    monkeypatch.setattr(provider, "read_relationships", lose_recipient_acknowledgement)

    def execute(actor):
        if operation == "transfer":
            return transfer_event_ownership(event.pk, actor, target_uuid)
        return assign_event_staff(event.pk, actor, target_uuid, "co_organizer")

    with pytest.raises(CTFStateError):
        execute(original)
    pending = CTFEventAuthorityTransition.objects.get(event=event, state="pending")
    Principal.objects.filter(uuid=target_uuid).update(is_active=False)
    monkeypatch.setattr(provider, "read_relationships", read)
    settled = execute(current)
    if operation == "transfer":
        assert settled.owner_principal_uuid == target_uuid
    else:
        assert settled.principal_uuid == target_uuid and settled.role == "co_organizer"
    pending.refresh_from_db()
    assert pending.state == "confirmed" and pending.actor_credential_uuid == original.credential_uuid
    assert AuthorizationOperation.objects.filter(
        idempotency_key__in=[effect["key"] for effect in pending.effect_plan["effects"]], state="confirmed"
    ).count() == len(pending.effect_plan["effects"])
    can_transfer = provider.check(
        AuthorizationRequest(
            original.principal,
            "event.transfer_ownership",
            TargetRef("event", event.pk),
            ResourceScope("account", account.uuid),
            original.ceiling,
        )
    ).allowed
    assert can_transfer is (operation != "transfer")
