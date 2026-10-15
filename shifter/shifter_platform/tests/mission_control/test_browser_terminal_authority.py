"""Browser terminal admission uses the live session, never historical ownership."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["principal_range"]
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture
def browser_range(principal_range, client, django_user_model, settings, monkeypatch):
    from cms.services import reassign_range_owner
    from engine.models import Request
    from management.services import principal_for_user
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from tests.engine.services.conftest import boto3_secrets, make_secrets_client
    from workspaces.services import add_account_member

    settings.SESSION_ENGINE = "management.session_backend"
    settings.CLOUD_PROVIDER = "aws"
    fixture = principal_range
    user = django_user_model.objects.get(username="synthetic-account-owner")
    human = principal_for_user(user)
    add_account_member(fixture.account.uuid, fixture.owner)
    add_account_member(fixture.account.uuid, human)
    for row in (
        fixture.instance,
        fixture.request,
        fixture.realized,
        Request.objects.get(pk=fixture.realized.request_id),
    ):
        row.user_id = user.pk
        row.save(update_fields=["user_id"])
    reassign_range_owner(fixture.instance.pk, human, credential=fixture.credential)
    fixture.realized.refresh_from_db()
    fixture.instance.refresh_from_db()
    member = str(uuid4())
    fixture.realized.provisioned_instances = [
        {
            "uuid": member,
            "os_type": "linux",
            "private_ip": "192.0.2.10",
            "ssh_username": "ubuntu",
            "ssh_key_secret_id": "synthetic-browser-key",
            "participant_access_channels": ["ssh"],
        }
    ]
    fixture.realized.save(update_fields=["provisioned_instances"])
    client.force_login(user, backend="config.auth.PlatformModelBackend")

    def check(request):
        from cms.models import RangeInstance

        owner = RangeInstance.objects.get(pk=fixture.instance.pk).owner_principal_uuid
        allowed = request.principal == fixture.credential.principal or request.principal.uuid == owner
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED, "policy_allowed" if allowed else "policy_denied"
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    from config.session_credentials import validate_socket_session

    scope = {"session": client.session, "path": f"/ws/terminal/{member}/"}
    assert validate_socket_session(scope)
    assert scope["credential_context"].principal == human
    secrets = make_secrets_client(value="SYNTHETIC_BROWSER_KEY")
    with boto3_secrets(secrets):
        yield SimpleNamespace(fixture=fixture, user=user, member=member, client=client, secrets=secrets)


def test_user_only_terminal_and_connection_info_reject_principal_ranges(browser_range):
    from engine.services import (
        connect_terminal,
        get_owned_instance_request_ref,
        get_rdp_connection_info,
        get_ssh_connection_info,
    )

    for operation in (
        get_owned_instance_request_ref,
        get_ssh_connection_info,
        get_rdp_connection_info,
        connect_terminal,
    ):
        with pytest.raises(PermissionError):
            operation(browser_range.user, browser_range.member)


def test_live_browser_credential_preserves_legacy_workspace_terminal_access(browser_range):
    from cms.models import Request as CmsRequest
    from cms.services import connect_range_terminal
    from config.session_credentials import session_credential
    from engine.models import Range, Request
    from workspaces.services import resolve_personal_workspace

    user = browser_range.user
    workspace_id = resolve_personal_workspace(user).workspace_id
    request_id, member = uuid4(), str(uuid4())
    CmsRequest.objects.create(request_id=request_id, request_type="range", user=user, workspace_id=workspace_id)
    native_request = Request.objects.create(request_id=request_id, request_type="range", user=user)
    Range.objects.create(
        request=native_request,
        user=user,
        workspace_id=workspace_id,
        status="ready",
        provisioned_instances=[
            {
                "uuid": member,
                "private_ip": "192.0.2.11",
                "os_type": "linux",
                "ssh_username": "ubuntu",
                "ssh_key_secret_id": "synthetic-browser-key",
                "participant_access_channels": ["ssh"],
            }
        ],
    )
    connection = connect_range_terminal(
        user,
        member,
        credential=session_credential(user, browser_range.client.session),
        connection_factory=lambda **parameters: parameters,
    )
    assert connection["host"] == "192.0.2.11"
    assert connection["private_key"] == "SYNTHETIC_BROWSER_KEY"


def test_service_terminal_admission_uses_its_credential_with_anonymous_framework_user(browser_range):
    from django.contrib.auth.models import AnonymousUser

    from config.socket_authority import authorize_socket_resource

    assert authorize_socket_resource(
        {
            "user": AnonymousUser(),
            "credential_context": browser_range.fixture.credential,
            "path": f"/ws/terminal/{browser_range.member}/",
        }
    )


def test_incumbent_browser_backend_cannot_authorize_principal_resources(browser_range, settings):
    from config.session_credentials import validate_socket_session
    from config.socket_authority import authorize_socket_resource

    settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    browser_range.client.force_login(browser_range.user, backend="config.auth.PlatformModelBackend")
    scope = {"session": browser_range.client.session, "path": f"/ws/terminal/{browser_range.member}/"}
    assert validate_socket_session(scope)
    assert "credential_context" not in scope
    with pytest.raises(PermissionError):
        authorize_socket_resource(scope)


@pytest.mark.asyncio
async def test_browser_session_terminal_revalidates_transfer_and_denies_former_owner(browser_range):
    from channels.auth import AuthMiddlewareStack
    from channels.db import database_sync_to_async
    from channels.routing import URLRouter
    from channels.testing import WebsocketCommunicator
    from django.urls import re_path

    from cms.services import reassign_range_owner
    from config.websocket_auth import CredentialSessionWebSocketBoundary
    from mission_control.consumers import SSHConsumer

    class Terminal:
        is_connected = True
        closed = False

        async def connect(self):
            pass

        async def disconnect(self):
            self.closed = True
            self.is_connected = False

        async def receive(self, timeout=0.1):
            await asyncio.sleep(timeout)
            return b""

    terminal = Terminal()
    application = AuthMiddlewareStack(
        CredentialSessionWebSocketBoundary(
            URLRouter(
                [
                    re_path(
                        r"^ws/terminal/(?P<instance_uuid>[^/]+)/$",
                        SSHConsumer.as_asgi(connection_factory=lambda **kw: terminal),
                    )
                ]
            ),
            recheck_seconds=0.05,
        )
    )
    cookie = browser_range.client.cookies["sessionid"].value
    path = f"/ws/terminal/{browser_range.member}/"
    headers = [(b"cookie", f"sessionid={cookie}".encode())]
    socket = WebsocketCommunicator(application, path, headers=headers)
    try:
        assert (await socket.connect(timeout=5))[0]
        browser_range.secrets.get_secret_value.assert_called_once_with(SecretId="synthetic-browser-key")
        fixture = browser_range.fixture
        await database_sync_to_async(reassign_range_owner)(
            fixture.instance.pk, fixture.owner, credential=fixture.credential
        )
        assert (await socket.receive_output(timeout=5))["type"] == "websocket.close"
        await socket.wait(timeout=5)
        assert terminal.closed
        rejected = WebsocketCommunicator(application, path, headers=headers)
        try:
            assert not (await rejected.connect(timeout=5))[0]
            await rejected.disconnect()
        finally:
            rejected.stop()
    finally:
        socket.stop()
