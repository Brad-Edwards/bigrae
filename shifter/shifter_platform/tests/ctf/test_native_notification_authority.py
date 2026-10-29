"""Native notification and restricted-scoreboard admission crosses real boundaries."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from ctf.services.event import assign_event_staff, revoke_event_staff, transfer_event_ownership
from ctf.services.notification.realtime import event_topic, publish_event_notification
from shared.authorization import (
    AuthorizationDecision,
    CredentialCeiling,
    DecisionKind,
    TargetRef,
    configured_authorization_provider,
)
from tests.ctf._api_flow_helpers import call_json
from tests.ctf.test_event_staff_api import helper_organizer as helper_organizer
from tests.integration.asgi.conftest import in_memory_channel_layer as in_memory_channel_layer

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("proof", ["live", "empty", "wrong_event", "expired"])
def test_subscriber_port_requires_live_exact_credential(ctf_event, organizer_credential, organizer_user, proof):
    from ctf.services.notification.realtime import register_ctf_notifications
    from management.models import PrincipalSession
    from shared.models import WebSocketNotification
    from shared.notifications import authorize_subscription, authorized_notification_payload

    credential = organizer_credential
    if proof == "empty":
        credential = replace(credential, ceiling=CredentialCeiling(frozenset()))
    elif proof == "wrong_event":
        credential = replace(
            credential, ceiling=CredentialCeiling(frozenset({"event.read"}), TargetRef("event", uuid4()))
        )
    elif proof == "expired":
        PrincipalSession.objects.filter(credential_uuid=credential.credential_uuid).update(
            expire_date=timezone.now() - timedelta(seconds=1)
        )
    register_ctf_notifications()
    assert authorize_subscription(credential, event_topic(ctf_event.pk)) is (proof == "live")
    row = WebSocketNotification.objects.create(
        recipient=organizer_user,
        notification_type="ctf_event",
        topic=event_topic(ctf_event.pk),
        payload={"kind": "first_blood", "body": "synthetic-canary"},
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    payload = authorized_notification_payload(credential, row)
    assert payload == ({"kind": "refresh"} if proof == "live" else None)


@pytest.mark.parametrize("actor", ["owner", "staff"])
def test_restricted_scoreboard_ignores_legacy_flags_when_native_read_is_denied(
    ctf_event, organizer_credential, organizer_user, helper_organizer, monkeypatch, actor
):
    ctf_event.scoreboard_visibility = "participants"
    ctf_event.save(update_fields=["scoreboard_visibility"])
    if actor == "staff":
        assign_event_staff(ctf_event.pk, organizer_credential, helper_organizer.identity_principal.uuid, "co_organizer")
    provider = configured_authorization_provider()
    check = provider.check

    def denied(request):
        if request.action == "event.read":
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return check(request)

    monkeypatch.setattr(provider, "check", denied)
    client = Client()
    client.force_login(helper_organizer if actor == "staff" else organizer_user)
    response = call_json(client, "get", "api_scoreboard", kwargs={"event_id": ctf_event.pk})
    assert response.status_code == 200
    assert response.json()["scoreboard_hidden"] is True


def test_current_native_human_recipients_need_no_group_and_services_are_not_browser_users(
    ctf_event,
    organizer_credential,
    organizer_user,
    admin_user,
    live_session_credential,
    settings,
    in_memory_channel_layer,
    monkeypatch,
):
    from django.db import transaction

    from management.services import create_service_principal
    from shared.models import WebSocketNotification

    colleague = get_user_model().objects.create_user(username="native-notification-colleague")
    assign_event_staff(ctf_event.pk, organizer_credential, colleague.identity_principal.uuid, "co_organizer")
    service = create_service_principal("Synthetic event owner", responsible_user=admin_user)
    assign_event_staff(ctf_event.pk, organizer_credential, service.uuid, "co_organizer")
    transfer_event_ownership(ctf_event.pk, organizer_credential, service.uuid)
    administrator = live_session_credential(admin_user)
    revoke_event_staff(ctf_event.pk, administrator, organizer_user.identity_principal.uuid)
    ctf_event.refresh_from_db()
    assert ctf_event.owner_principal_uuid == service.uuid
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = True
    provider = configured_authorization_provider()

    def remote_call_is_forbidden(request):
        raise AssertionError("Recipient projection must not perform remote policy I/O")

    monkeypatch.setattr(provider, "check", remote_call_is_forbidden)
    with transaction.atomic():
        publish_event_notification(ctf_event, "first_blood", {})
    assert set(
        WebSocketNotification.objects.filter(topic=event_topic(ctf_event.pk)).values_list("recipient_id", flat=True)
    ) == {colleague.pk}


@pytest.mark.parametrize("status,registered", [("banned", True), ("active", False)])
def test_restricted_scoreboard_requires_eligible_registered_participant(
    ctf_event, participant_user, status, registered, monkeypatch
):
    from ctf.models import CTFParticipant

    ctf_event.scoreboard_visibility = "participants"
    ctf_event.save(update_fields=["scoreboard_visibility"])
    CTFParticipant.objects.create(
        event=ctf_event,
        user=participant_user,
        name="Synthetic member",
        status=status,
        registered_at=timezone.now() if registered else None,
    )
    provider = configured_authorization_provider()
    monkeypatch.setattr(
        provider,
        "check",
        lambda request: AuthorizationDecision(
            DecisionKind.ALLOWED if request.action == "event.participate" else DecisionKind.DENIED,
            "policy_allowed" if request.action == "event.participate" else "policy_denied",
        ),
    )
    client = Client()
    client.force_login(participant_user)
    response = call_json(client, "get", "api_scoreboard", kwargs={"event_id": ctf_event.pk})
    assert response.status_code == 200 and response.json()["scoreboard_hidden"] is True


@pytest.mark.parametrize("kind", ["human", "service"])
@pytest.mark.parametrize("participate,ceiling", [(True, True), (False, True), (True, False)])
def test_participant_read_requires_independent_policy_and_ceiling(
    ctf_event, live_session_credential, monkeypatch, kind, participate, ceiling
):
    from rest_framework.test import APIClient

    from ctf.models import CTFParticipant
    from tests.workspaces.test_account_policy_admin import _actor

    actor = _actor(kind, actions=frozenset({"event.read", "event.participate"} if ceiling else {"event.read"}))
    user = get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid) if kind == "human" else None
    CTFParticipant.objects.create(
        event=ctf_event,
        user=user,
        principal_uuid=actor.principal.uuid,
        name="Native member",
        status="disqualified",
        registered_at=timezone.now(),
    )
    ctf_event.status = "ended"
    ctf_event.scoreboard_visibility = "participants"
    ctf_event.save(update_fields=["status", "scoreboard_visibility"])
    provider = configured_authorization_provider()
    monkeypatch.setattr(
        provider,
        "check",
        lambda request: AuthorizationDecision(
            DecisionKind.ALLOWED if request.action == "event.participate" and participate else DecisionKind.DENIED,
            "policy_allowed" if request.action == "event.participate" and participate else "policy_denied",
        ),
    )
    client = APIClient()
    client.force_authenticate(user=user, token=actor)
    response = call_json(client, "get", "api_scoreboard", kwargs={"event_id": ctf_event.pk})
    assert response.status_code == 200
    assert response.json()["scoreboard_hidden"] is not (participate and ceiling)


def test_read_provider_outage_cannot_fall_back_to_membership(
    ctf_event, organizer_credential, organizer_user, monkeypatch
):
    from ctf.models import CTFParticipant

    CTFParticipant.objects.create(
        event=ctf_event, user=organizer_user, name="Native owner member", status="active", registered_at=timezone.now()
    )
    ctf_event.scoreboard_visibility = "participants"
    ctf_event.save(update_fields=["scoreboard_visibility"])
    provider = configured_authorization_provider()
    monkeypatch.setattr(
        provider,
        "check",
        lambda request: AuthorizationDecision(
            DecisionKind.EVALUATOR_ERROR if request.action == "event.read" else DecisionKind.ALLOWED,
            "evaluator_unavailable" if request.action == "event.read" else "policy_allowed",
        ),
    )
    client = Client()
    client.force_login(organizer_user)
    response = call_json(client, "get", "api_scoreboard", kwargs={"event_id": ctf_event.pk})
    assert response.status_code == 503
