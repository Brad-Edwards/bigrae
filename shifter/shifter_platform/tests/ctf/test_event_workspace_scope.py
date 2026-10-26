"""CTF event placement uses explicit, immutable account ancestry (ADR-066)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from ctf.exceptions import CTFStateError, CTFValidationError
from ctf.models import CTFEvent
from ctf.services.event import create_event, update_event
from tests.ctf._account_scope import individual_account_uuid
from workspaces.models import Account

pytestmark = pytest.mark.django_db


def _event_data(**overrides):
    now = timezone.now()
    data = {
        "name": "Scoped Event",
        "event_start": now + timedelta(days=1),
        "event_end": now + timedelta(days=1, hours=8),
    }
    data.update(overrides)
    return data


def test_create_event_defaults_to_creator_individual_account(organizer_user):
    account_uuid = individual_account_uuid(organizer_user)

    event = create_event(organizer_user, _event_data())

    assert event.scope_kind == "account"
    assert str(Account.objects.get(pk=event.account_id).uuid) == account_uuid
    assert event.organization_id is None
    assert event.workspace_id is None


def test_create_event_uses_an_explicit_authorized_account(organizer_user):
    account_uuid = individual_account_uuid(organizer_user)

    event = create_event(organizer_user, _event_data(account=UUID(account_uuid)))

    assert str(Account.objects.get(pk=event.account_id).uuid) == account_uuid


def test_create_event_rejects_an_unknown_account(organizer_user):
    with pytest.raises(CTFValidationError):
        create_event(organizer_user, _event_data(account=uuid4()))


def test_create_event_rejects_an_existing_foreign_account(organizer_user, django_user_model):
    foreign = django_user_model.objects.create_user("foreign-scope-owner@example.test")
    foreign_account = Account.objects.create(
        kind=Account.Kind.INDIVIDUAL,
        name="Foreign account",
        individual_principal_uuid=foreign.identity_principal.uuid,
    )

    with pytest.raises(CTFValidationError):
        create_event(organizer_user, _event_data(account=foreign_account.uuid))


def test_event_account_scope_is_immutable_once_set(ctf_event):
    reloaded = CTFEvent.objects.get(pk=ctf_event.pk)
    other = Account.objects.create(kind=Account.Kind.TEAM, name="Other account")
    reloaded.account_id = other.pk

    with pytest.raises(ValidationError):
        reloaded.save()


def test_service_created_event_always_carries_a_scope(organizer_user):
    event = create_event(organizer_user, _event_data())

    assert event.scope_kind == "account"
    assert event.account_id is not None
    assert event.organization_id is None
    assert event.workspace_id is None


def test_created_event_scope_cannot_be_rebound_without_reload(organizer_user):
    event = create_event(organizer_user, _event_data())
    other = Account.objects.create(kind=Account.Kind.TEAM, name="Other account")
    event.account_id = other.pk

    with pytest.raises(ValidationError):
        event.save()


def test_deferred_load_event_scope_cannot_be_rebound(ctf_event):
    deferred = CTFEvent.objects.only("id", "name").get(pk=ctf_event.pk)
    other = Account.objects.create(kind=Account.Kind.TEAM, name="Other account")
    deferred.account_id = other.pk

    with pytest.raises(ValidationError):
        deferred.save()


def test_update_rejects_a_different_account_instead_of_ignoring_it(ctf_event):
    own_uuid = Account.objects.get(pk=ctf_event.account_id).uuid
    other = Account.objects.create(kind=Account.Kind.TEAM, name="Other event account")

    with pytest.raises(CTFStateError, match="cannot change its placement"):
        update_event(ctf_event.pk, {"account": other.uuid}, actor_id=ctf_event.created_by_id)
    ctf_event.refresh_from_db()
    assert Account.objects.get(pk=ctf_event.account_id).uuid == own_uuid
    assert (
        update_event(ctf_event.pk, {"account": own_uuid}, actor_id=ctf_event.created_by_id).account_id
        == ctf_event.account_id
    )
