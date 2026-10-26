"""S5 CTF policy seam: principal authority is independent of event creator."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from ctf.exceptions import CTFPermissionError
from management.services import create_service_principal
from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, TargetRef
from shared.credentials import CredentialContext


@pytest.mark.django_db
def test_service_administrator_uses_live_event_policy_and_exact_scope(ctf_event_active, monkeypatch):
    from ctf.models import CTFEvent
    from ctf.services.unified_authorization import require_event_action, require_event_creation
    from shared.authorization import port
    from workspaces.models import Account

    event = CTFEvent.objects.create(
        name="Personal CTF event",
        created_by=ctf_event_active.created_by,
        scope_kind="account",
        account_id=ctf_event_active.account_id,
        scenario_id="synthetic",
        event_start=ctf_event_active.event_start,
        event_end=ctf_event_active.event_end,
    )
    account = Account.objects.get(pk=event.account_id)
    service = create_service_principal("CTF administrator")
    credential = CredentialContext(
        service,
        "service",
        uuid4(),
        CredentialCeiling(frozenset({"event.manage_staff", "account.create_event"})),
        frozenset(),
    )
    requests = []
    allowed = True

    def check(request):
        requests.append(request)
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
            "policy_allowed" if allowed else "policy_denied",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    require_event_action(credential, event.pk, "event.manage_staff")
    assert requests[-1].target == TargetRef("event", event.pk)
    assert requests[-1].scope.account_uuid == account.uuid
    assert requests[-1].scope.organization_uuid is None
    assert requests[-1].scope.workspace_uuid is None
    assert requests[-1].principal == service
    placement = require_event_creation(credential, TargetRef("account", account.uuid))
    assert placement.account_uuid == account.uuid
    assert placement.organization_uuid is None
    assert requests[-1].action == "account.create_event"
    assert requests[-1].target == TargetRef("account", account.uuid)
    allowed = False
    with pytest.raises(CTFPermissionError):
        require_event_action(credential, event.pk, "event.manage_staff")
    with pytest.raises(CTFPermissionError):
        require_event_action(credential, uuid4(), "event.manage_staff")

    def unavailable(_request):
        raise RuntimeError("provider secret must stay private")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=unavailable))
    from ctf.exceptions import CTFError

    with pytest.raises(CTFError, match="authority unavailable") as failed:
        require_event_action(credential, event.pk, "event.manage_staff")
    assert "secret" not in str(failed.value)


@pytest.mark.django_db
def test_temporary_participant_cannot_use_event_administration(ctf_event_active, organizer_credential):
    from config.credential_scope import temporary_participant_credential
    from ctf.services.participant.accounts import create_participant_accounts
    from ctf.services.unified_authorization import require_event_action

    participant = create_participant_accounts(ctf_event_active.pk, count=1, actor=organizer_credential)[0]
    credential = temporary_participant_credential(participant.user)
    with pytest.raises(CTFPermissionError):
        require_event_action(credential, participant.event_id, "event.manage_staff")


@pytest.mark.django_db
def test_service_creation_uses_business_parent_ancestry(monkeypatch):
    from ctf.services.unified_authorization import require_event_creation
    from shared.authorization import port
    from workspaces.models import Organization, Workspace
    from workspaces.services import create_account

    account = create_account(kind="team", name="Business event account")
    organization = Organization.objects.get(account_id=account.id, is_default=True)
    workspace = Workspace.objects.get(organization=organization, is_default=True)
    service = create_service_principal("Event creator")
    credential = CredentialContext(
        service,
        "service",
        uuid4(),
        CredentialCeiling(frozenset({"organization.create_event", "workspace.create_event"})),
        frozenset(),
    )
    seen = []

    def check(request):
        seen.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    for target in (TargetRef("organization", organization.uuid), TargetRef("workspace", workspace.uuid)):
        scope = require_event_creation(credential, target)
        assert scope.account_uuid == account.uuid
        assert scope.organization_uuid == organization.uuid
        assert scope.workspace_uuid == (workspace.uuid if target.type == "workspace" else None)
        assert seen[-1].target == target
    with pytest.raises(CTFPermissionError):
        require_event_creation(credential, TargetRef("account", account.uuid))
    with pytest.raises(CTFPermissionError):
        require_event_creation(credential, TargetRef("account", uuid4()))


@pytest.mark.django_db
def test_business_event_cannot_claim_a_direct_account_parent(ctf_event_active, monkeypatch):
    from ctf.models import CTFEvent
    from ctf.services.unified_authorization import require_event_action
    from shared.authorization import port
    from workspaces.services import create_account

    account = create_account(kind="team", name="Business account")
    event = CTFEvent.objects.create(
        name="Invalid placement",
        created_by=ctf_event_active.created_by,
        scope_kind="account",
        account_id=account.id,
        scenario_id="synthetic",
        event_start=ctf_event_active.event_start,
        event_end=ctf_event_active.event_end,
    )
    service = create_service_principal("Administrator")
    credential = CredentialContext(
        service, "service", uuid4(), CredentialCeiling(frozenset({"event.manage_config"})), frozenset()
    )
    checked = []

    def check(request):
        checked.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))

    with pytest.raises(CTFPermissionError):
        require_event_action(credential, event.pk, "event.manage_config")
    assert checked == []


@pytest.mark.django_db
def test_service_principal_creates_updates_and_delegates_event_without_human_projection(monkeypatch):
    """Service administrators use the same parent/event policy and principal topology."""
    from datetime import timedelta

    from django.utils import timezone

    from ctf.enums import EventStaffRole
    from ctf.services.event import assign_event_staff, create_event_for_credential, update_event
    from ctf.services.event.staff import actions_for_role
    from management.services import create_service_principal
    from shared.authorization import port
    from workspaces.models import Organization
    from workspaces.services import create_account

    account = create_account(kind="team", name="Service event account")
    organization = Organization.objects.get(account_id=account.id, is_default=True)
    owner = create_service_principal("Service event owner")
    delegate = create_service_principal("Service event delegate")
    credential = CredentialContext(
        owner,
        "service",
        uuid4(),
        CredentialCeiling(
            frozenset(
                {
                    "organization.create_event",
                    "event.manage",
                    "event.manage_staff",
                    *actions_for_role(EventStaffRole.CO_ORGANIZER.value),
                }
            )
        ),
        frozenset(),
    )
    checked = []

    def check(request):
        checked.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    relationships = set()

    def write_relationships(change):
        from shared.authorization import PolicyEffect

        relationships.update((row.user, row.relation, row.object) for row in change.writes)
        logical = change.grant_tuple
        logical_key = (logical.user, logical.relation, logical.object)
        if change.effect == PolicyEffect.GRANT:
            relationships.add(logical_key)
        else:
            relationships.discard(logical_key)

    def read_relationships(change):
        from shared.authorization import RelationshipState

        row = change.grant_tuple
        return RelationshipState((row.user, row.relation, row.object) in relationships, False)

    provider = SimpleNamespace(
        store_id="test-store",
        model_id="test-model",
        check=check,
        batch_check=lambda requests: tuple(check(request) for request in requests),
        write_relationships=write_relationships,
        read_relationships=read_relationships,
    )
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    now = timezone.now()
    event = create_event_for_credential(
        credential,
        {
            "name": "Principal-native event",
            "event_start": now + timedelta(days=1),
            "event_end": now + timedelta(days=2),
            "organization": organization.uuid,
        },
    )

    assert event.created_by_id is None
    assert event.creator_principal_uuid == owner.uuid
    assert event.owner_principal_uuid == owner.uuid
    assert event.authorization_publication_state == "confirmed"
    assert relationships == {
        (f"organization:{organization.uuid}", "organization", f"event:{event.pk}"),
        (f"principal:{owner.uuid}", "direct_administrator", f"event:{event.pk}"),
    }
    assert checked[-1].action == "organization.create_event"

    update_event(event.pk, {"description": "updated"}, actor_id=credential)
    assert checked[-1].action == "event.manage_config"
    staff_checks_start = len(checked)
    staff = assign_event_staff(event.pk, credential, delegate.uuid, EventStaffRole.CO_ORGANIZER.value)

    assert staff.user_id is None
    assert staff.principal_uuid == delegate.uuid
    delegated_actions = [request.action for request in checked[staff_checks_start:]]
    assert set(actions_for_role(EventStaffRole.CO_ORGANIZER.value)) <= set(delegated_actions)
    assert delegated_actions.count("event.manage") == len(actions_for_role(EventStaffRole.CO_ORGANIZER.value))


@pytest.mark.django_db(transaction=True)
def test_event_relationship_publication_recovers_lost_acknowledgement(monkeypatch):
    """A provider timeout after commit is reconciled by primary readback."""
    from datetime import timedelta

    from django.utils import timezone

    from ctf.services.event import create_event_for_credential
    from management.services import create_service_principal
    from shared.authorization import RelationshipState, port
    from workspaces.models import Organization
    from workspaces.services import create_account

    account = create_account(kind="team", name="Lost acknowledgement account")
    organization = Organization.objects.get(account_id=account.id, is_default=True)
    owner = create_service_principal("Lost acknowledgement owner")
    credential = CredentialContext(
        owner,
        "service",
        uuid4(),
        CredentialCeiling(frozenset({"organization.create_event"})),
        frozenset(),
    )
    relationships = set()
    first = True
    provider_io_in_atomic = []

    def check(_request):
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    def write_relationships(change):
        nonlocal first
        from django.db import connection

        provider_io_in_atomic.append(connection.in_atomic_block)
        relationships.update((row.user, row.relation, row.object) for row in change.writes)
        if first:
            first = False
            raise RuntimeError("lost acknowledgement")

    def read_relationships(change):
        row = change.grant_tuple
        return RelationshipState((row.user, row.relation, row.object) in relationships, False)

    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(
            store_id="test-store",
            model_id="test-model",
            check=check,
            write_relationships=write_relationships,
            read_relationships=read_relationships,
        ),
    )
    now = timezone.now()
    event = create_event_for_credential(
        credential,
        {
            "name": "Lost acknowledgement event",
            "event_start": now + timedelta(days=1),
            "event_end": now + timedelta(days=2),
            "organization": organization.uuid,
        },
    )

    assert event.authorization_publication_state == "confirmed"
    assert event.authorization_provider_store_id == "test-store"
    assert event.authorization_model_id == "test-model"
    assert provider_io_in_atomic == [False, False]
