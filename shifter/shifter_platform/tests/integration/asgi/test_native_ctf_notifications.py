"""Current native topic authority through the real ASGI/session/channel stack."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.utils import timezone

from shared.authorization import AuthorizationDecision, DecisionKind
from shared.channels.groups import notification_user_topic_group
from shared.enums import WebSocketCloseCode

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def native_notice(ws_user, ws_cookie_value, settings, monkeypatch):
    from config.session_credentials import session_credential
    from ctf.models import CTFEvent
    from ctf.services.notification.realtime import event_topic, register_ctf_notifications
    from management.services import principal_for_user
    from management.session_backend import SessionStore
    from shared.authorization import port
    from workspaces.services import create_account

    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = True
    principal = principal_for_user(ws_user)
    account = create_account(kind="individual", name="Synthetic notification account", owner=principal)
    now = timezone.now()
    event = CTFEvent.objects.create(
        name="Synthetic notification event",
        created_by=ws_user,
        owner_principal_uuid=principal.uuid,
        scope_kind="account",
        account_id=account.id,
        scenario_id="synthetic-notification",
        status="active",
        event_start=now - timedelta(hours=1),
        event_end=now + timedelta(hours=1),
    )
    policy = {"event.read": DecisionKind.ALLOWED, "event.participate": DecisionKind.DENIED}

    def check(request):
        assert request.principal == principal
        assert request.target.uuid == event.pk and request.scope.account_uuid == account.uuid
        kind = policy[request.action]
        reason = {
            DecisionKind.ALLOWED: "policy_allowed",
            DecisionKind.DENIED: "policy_denied",
            DecisionKind.EVALUATOR_ERROR: "evaluator_unavailable",
        }[kind]
        return AuthorizationDecision(kind, reason)

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    register_ctf_notifications()
    credential = session_credential(ws_user, SessionStore(session_key=ws_cookie_value), provider_check=False)
    return SimpleNamespace(event=event, topic=event_topic(event.pk), policy=policy, credential=credential, user=ws_user)


def notice(case):
    from shared.models import WebSocketNotification

    return WebSocketNotification.objects.create(
        recipient=case.user,
        notification_type="ctf_event",
        topic=case.topic,
        payload={"kind": "first_blood", "body": "synthetic-private-canary"},
        expires_at=timezone.now() + timedelta(minutes=5),
    )


async def connect(application, headers):
    socket = WebsocketCommunicator(application, "/ws/notifications/", headers=headers)
    connected, _ = await socket.connect()
    assert connected is True
    return socket


async def subscribe(socket, topic):
    await socket.send_json_to({"type": "subscribe", "topic": topic})
    assert await socket.receive_json_from() == {"type": "subscribed", "topic": topic}


async def dispatch(case, row):
    await get_channel_layer().group_send(
        notification_user_topic_group(case.user.pk, case.topic),
        {"type": "notification.dispatch", "notification_id": row.pk},
    )


@pytest.mark.parametrize("outcome", [DecisionKind.DENIED, DecisionKind.EVALUATOR_ERROR])
async def test_initial_subscription_and_pending_replay_recheck_policy(
    asgi_application, ws_headers, native_notice, in_memory_channel_layer, outcome
):
    pending = await sync_to_async(notice)(native_notice)
    socket = await connect(asgi_application, ws_headers)
    try:
        native_notice.policy["event.read"] = outcome
        await socket.send_json_to({"type": "subscribe", "topic": native_notice.topic})
        output = await socket.receive_output()
        assert output["type"] == "websocket.close" and output["code"] == WebSocketCloseCode.PERMISSION_DENIED
        await sync_to_async(pending.refresh_from_db)()
        assert pending.delivered_at is None
    finally:
        await socket.disconnect()


async def test_live_delivery_rechecks_policy_and_preserves_pending_until_allowed(
    asgi_application, ws_headers, native_notice, in_memory_channel_layer
):
    socket = await connect(asgi_application, ws_headers)
    try:
        await subscribe(socket, native_notice.topic)
        pending = await sync_to_async(notice)(native_notice)
        native_notice.policy["event.read"] = DecisionKind.DENIED
        await dispatch(native_notice, pending)
        assert await socket.receive_nothing(timeout=0.2)
        await sync_to_async(pending.refresh_from_db)()
        assert pending.delivered_at is None
        native_notice.policy["event.read"] = DecisionKind.ALLOWED
        await dispatch(native_notice, pending)
        payload = await socket.receive_json_from()
        assert payload["id"] == pending.pk and payload["payload"] == {"kind": "refresh"}
        assert "synthetic-private-canary" not in str(payload)
    finally:
        await socket.disconnect()


async def test_expired_native_cookie_cannot_replay_or_deliver(
    asgi_application, ws_headers, native_notice, in_memory_channel_layer
):
    from management.models import PrincipalSession

    socket = await connect(asgi_application, ws_headers)
    try:
        await subscribe(socket, native_notice.topic)
        pending = await sync_to_async(notice)(native_notice)
        await sync_to_async(
            lambda: PrincipalSession.objects.filter(credential_uuid=native_notice.credential.credential_uuid).update(
                expire_date=timezone.now() - timedelta(seconds=1)
            )
        )()
        await socket.send_json_to({"type": "subscribe", "topic": native_notice.topic})
        output = await socket.receive_output()
        assert output["type"] == "websocket.close" and output["code"] == WebSocketCloseCode.NOT_AUTHENTICATED
        await sync_to_async(pending.refresh_from_db)()
        assert pending.delivered_at is None
    finally:
        await socket.disconnect()


async def test_revocation_at_final_send_preserves_undelivered_receipt(
    asgi_application, ws_headers, native_notice, in_memory_channel_layer, monkeypatch
):
    from management.models import PrincipalSession
    from shared.authorization import configured_authorization_provider

    socket = await connect(asgi_application, ws_headers)
    try:
        await subscribe(socket, native_notice.topic)
        pending = await sync_to_async(notice)(native_notice)
        provider = configured_authorization_provider()
        check = provider.check

        def revoke_after_policy(request):
            decision = check(request)
            PrincipalSession.objects.filter(credential_uuid=native_notice.credential.credential_uuid).delete()
            return decision

        monkeypatch.setattr(provider, "check", revoke_after_policy)
        # The fixture returns a fresh provider wrapper; retain this same actual
        # evaluator port while injecting the external decision/commit race.
        monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
        await dispatch(native_notice, pending)
        output = await socket.receive_output()
        assert output["type"] == "websocket.close" and output["code"] == WebSocketCloseCode.NOT_AUTHENTICATED
        await sync_to_async(pending.refresh_from_db)()
        assert pending.delivered_at is None
    finally:
        await socket.disconnect()


@pytest.mark.parametrize("defect", ["banned", "unregistered"])
async def test_participant_delivery_rechecks_exact_current_membership(
    asgi_application, ws_headers, native_notice, in_memory_channel_layer, defect
):
    from ctf.models import CTFParticipant

    member = await sync_to_async(CTFParticipant.objects.create)(
        event=native_notice.event,
        user=native_notice.user,
        name="Synthetic native participant",
        status="active",
        registered_at=timezone.now(),
    )
    native_notice.policy.update({"event.read": DecisionKind.DENIED, "event.participate": DecisionKind.ALLOWED})
    socket = await connect(asgi_application, ws_headers)
    try:
        await subscribe(socket, native_notice.topic)
        pending = await sync_to_async(notice)(native_notice)
        changes = {"status": "banned"} if defect == "banned" else {"registered_at": None}
        await sync_to_async(lambda: CTFParticipant.objects.filter(pk=member.pk).update(**changes))()
        await dispatch(native_notice, pending)
        assert await socket.receive_nothing(timeout=0.2)
        await sync_to_async(pending.refresh_from_db)()
        assert pending.delivered_at is None
    finally:
        await socket.disconnect()
