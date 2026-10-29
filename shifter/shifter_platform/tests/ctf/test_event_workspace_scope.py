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

    from ctf.exceptions import CTFPermissionError

    with pytest.raises(CTFPermissionError):
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


def test_invalid_source_placement_creates_no_event(organizer_credential):
    from ctf.services.event import create_event_for_credential

    before = CTFEvent.objects.count()
    with pytest.raises(CTFValidationError, match="model sources"):
        create_event_for_credential(
            organizer_credential,
            _event_data(
                model_sources={
                    "aliases": [
                        {
                            "logical_alias": "helper",
                            "sources": [{"source_id": str(uuid4()), "revision": 1}],
                        }
                    ]
                }
            ),
        )
    assert CTFEvent.objects.count() == before


@pytest.mark.parametrize("recovery", ["creator", "administrator"])
def test_pending_event_initialization_recovers_with_current_authority_after_original_expiry(
    organizer_user,
    organizer_credential,
    admin_user,
    live_session_credential,
    monkeypatch,
    recovery,
):
    from ctf.services.event import create_event_for_credential
    from ctf.services.event.initialization import resume_event_initialization
    from management.models import PrincipalSession
    from shared.authorization import AuthorizationProviderError, configured_authorization_provider

    provider = configured_authorization_provider()
    original = provider.read_relationships

    def unavailable(change):
        raise AuthorizationProviderError("synthetic readback unavailable")

    monkeypatch.setattr(provider, "read_relationships", unavailable)
    with pytest.raises(CTFStateError):
        create_event_for_credential(organizer_credential, _event_data())
    pending = CTFEvent.objects.get(name="Scoped Event")
    PrincipalSession.objects.filter(credential_uuid=organizer_credential.credential_uuid).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    monkeypatch.setattr(provider, "read_relationships", original)
    with pytest.raises(CTFStateError):
        resume_event_initialization(pending.pk)
    current = live_session_credential(organizer_user if recovery == "creator" else admin_user)
    completed = resume_event_initialization(pending.pk, recovery_credential=current)
    assert completed.initialization_intent is None
    assert completed.owner_principal_uuid == organizer_credential.principal.uuid


def test_current_owner_can_abort_an_invalid_pending_initialization(organizer_credential):
    from django.contrib.auth import get_user_model

    from ctf.services.event.initialization import abort_event_initialization
    from management.services import human_principal_user_id
    from shared.model_access.sources import ModelSourceCredentialReference
    from tests.ctf.test_content_hydration import _event

    user = get_user_model().objects.get(pk=human_principal_user_id(organizer_credential.principal))
    pending = _event(user)
    pending.initialization_intent = {
        "authority": ModelSourceCredentialReference.from_credential(organizer_credential).model_dump(mode="json"),
        "model_sources": {
            "aliases": [{"logical_alias": "helper", "sources": [{"source_id": str(uuid4()), "revision": 1}]}]
        },
        "content_digest": None,
    }
    pending.save(update_fields=["initialization_intent"])
    abort_event_initialization(pending.pk, organizer_credential)
    assert not CTFEvent.all_objects.filter(pk=pending.pk).exists()
