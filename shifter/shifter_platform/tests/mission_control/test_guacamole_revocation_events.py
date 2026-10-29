"""Delivered native sessions terminate when their original access is revoked."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from tests.mission_control.test_guacamole_termination import client as native_client
from tests.mission_control.test_guacamole_termination import server
from tests.mission_control.test_principal_guacamole import principal_range, remote, transferable_range

__all__ = ["principal_range", "remote", "transferable_range"]
pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]


@pytest.fixture
def browser_remote(remote, transferable_range, django_user_model, settings):
    """Use a real browser proof and matching native workspace custody."""
    from django.test import Client

    from config.session_credentials import session_credential
    from management.services import principal_for_user

    actor, member, gateway = remote
    create_ssh_url = gateway.create_ssh_url

    def create_unique_session(request):
        return create_ssh_url(request).replace("SYNTHETIC_NATIVE_TOKEN", "SYNTHETIC_NATIVE_" + uuid4().hex)

    gateway.create_ssh_url = create_unique_session
    destination = transferable_range.destination
    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="native-remote-participant")
    browser = Client()
    browser.force_login(user, backend="config.auth.PlatformModelBackend")
    actor.credential = session_credential(user, browser.session, provider_check=False)
    actor.owner = principal_for_user(user)
    actor.account = destination.account
    for row in (actor.instance, actor.request, actor.realized):
        row.scope_kind = "account"
        row.account_id = destination.account.id
        row.organization_id = destination.organization.id
        row.workspace_id = destination.workspace.id
        row.owner_principal_uuid = actor.owner.uuid
        row.save(update_fields=["scope_kind", "account_id", "organization_id", "workspace_id", "owner_principal_uuid"])
    native_request = actor.realized.request
    native_request.owner_principal_uuid = actor.owner.uuid
    native_request.save(update_fields=["owner_principal_uuid"])
    actor.user, actor.browser = user, browser
    return actor, member, gateway


@pytest.fixture
def participant_remote(browser_remote):
    """Associate the exact native range with an ordinary human participant."""
    from ctf.models import CTFEvent, CTFParticipant

    actor, _, _ = browser_remote
    now = timezone.now()
    actor.event = CTFEvent.objects.create(
        name="Synthetic remote participation",
        creator_principal_uuid=actor.owner.uuid,
        owner_principal_uuid=actor.owner.uuid,
        scope_kind="account",
        account_id=actor.account.id,
        organization_id=actor.instance.organization_id,
        workspace_id=actor.instance.workspace_id,
        status="active",
        event_start=now - timedelta(hours=1),
        event_end=now + timedelta(hours=1),
    )
    actor.participant = CTFParticipant.objects.create(
        event=actor.event,
        user=actor.user,
        principal_uuid=actor.owner.uuid,
        name="Synthetic participant",
        status="active",
        registered_at=now,
        range_instance_id=actor.instance.pk,
        range_request_id=actor.request.request_id,
        range_status="ready",
    )
    actor.instance.event_uuid = actor.realized.event_uuid = actor.event.pk
    actor.instance.range_source = "ctf"
    actor.instance.save(update_fields=["event_uuid", "range_source"])
    actor.realized.save(update_fields=["event_uuid"])
    return browser_remote


def _deliver(remote):
    from mission_control.guacamole_bootstrap import consume_ready_url
    from mission_control.models import GuacamoleBootstrapRequest
    from tests.mission_control.test_principal_guacamole import launch

    actor, _, _ = remote
    submitted = launch(remote)
    row = GuacamoleBootstrapRequest.objects.get(pk=submitted.bootstrap_id)
    assert row.status == "succeeded", row.error_message
    assert consume_ready_url(request_id=row.pk, credential=actor.credential)
    row.refresh_from_db()
    assert row.delivered_at is not None and row.native_session_token
    return row


def _assert_native_termination(row):
    from mission_control.guacamole_authority import renew_gateway_authority, revalidate_guacamole_sessions

    token = row.native_session_token
    assert not renew_gateway_authority(str(row.pk))
    with server() as (url, received):
        assert revalidate_guacamole_sessions(client=native_client(url)) == 1
    assert received == [("/guacamole/api/session", token)]
    row.refresh_from_db()
    assert row.native_terminated_at is not None
    assert row.native_session_token == "" and not row.termination_pending


@pytest.mark.parametrize(
    "change", ["logout", "user_disabled", "user_deleted", "principal_disabled", "workspace_archived"]
)
def test_delivered_guacamole_terminates_after_browser_access_revocation(browser_remote, change):
    from management.models import Principal, PrincipalSession
    from mission_control.guacamole_authority import renew_gateway_authority

    actor, _, _ = browser_remote
    row = _deliver(browser_remote)
    assert renew_gateway_authority(str(row.pk))
    if change == "logout":
        response = actor.browser.post("/logout/")
        assert response.status_code == 302
        assert not PrincipalSession.objects.filter(credential_uuid=actor.credential.credential_uuid).exists()
    elif change == "user_disabled":
        actor.user.is_active = False
        actor.user.save(update_fields=["is_active"])
    elif change == "user_deleted":
        from management.services import mark_user_deleted

        mark_user_deleted(actor.user, strict=True)
    elif change == "principal_disabled":
        Principal.objects.filter(uuid=actor.owner.uuid).update(is_active=False)
    else:
        from shared.authorization.port import configured_authorization_provider
        from workspaces.services import admin_archive_workspace

        admin_archive_workspace(actor.credential, configured_authorization_provider(), actor.destination.workspace.uuid)
    _assert_native_termination(row)


@pytest.mark.parametrize("change", ["ban", "end", "deadline", "deleted", "unassigned"])
def test_delivered_participant_guacamole_terminates_after_losing_event_access(participant_remote, change):
    from ctf.services.event import end_event
    from ctf.services.participant.moderation import ban_participant
    from mission_control.guacamole_authority import renew_gateway_authority

    actor, _, _ = participant_remote
    row = _deliver(participant_remote)
    assert renew_gateway_authority(str(row.pk))
    if change == "ban":
        ban_participant(actor.participant.pk)
    elif change == "end":
        end_event(actor.event.pk)
    elif change == "deadline":
        actor.event.event_end = timezone.now() - timedelta(seconds=1)
        actor.event.save(update_fields=["event_end"])
    elif change == "deleted":
        actor.participant.delete()
    else:
        actor.participant.range_instance_id = None
        actor.participant.save(update_fields=["range_instance_id"])
    _assert_native_termination(row)


def test_healthy_participant_and_independent_operator_keep_their_native_sessions(
    participant_remote, django_user_model, live_session_credential
):
    from ctf.services.participant.moderation import ban_participant
    from mission_control.guacamole_authority import renew_gateway_authority, revalidate_guacamole_sessions

    actor, member, gateway = participant_remote
    participant_row = _deliver(participant_remote)
    assert renew_gateway_authority(str(participant_row.pk))
    with server() as (url, received):
        assert revalidate_guacamole_sessions(client=native_client(url)) == 0
    assert received == []
    operator = django_user_model.objects.create_user(username="independent-remote-operator")
    actor.credential = live_session_credential(operator)
    operator_row = _deliver((actor, member, gateway))
    ban_participant(actor.participant.pk)
    assert renew_gateway_authority(str(operator_row.pk))
    with server() as (url, received):
        assert revalidate_guacamole_sessions(client=native_client(url)) == 1
    assert received == [("/guacamole/api/session", participant_row.native_session_token)]
    operator_row.refresh_from_db()
    assert operator_row.native_terminated_at is None and operator_row.native_session_token


@pytest.mark.parametrize("failure", ["missing_callback", "participant_store_unavailable"])
def test_delivered_participant_session_fails_closed_when_standing_cannot_be_checked(
    participant_remote, monkeypatch, failure
):
    from django.db import OperationalError, connection

    from shared import range_workspace_aggregate

    row = _deliver(participant_remote)
    if failure == "missing_callback":
        monkeypatch.setattr(range_workspace_aggregate, "_remote_participation", None)
        _assert_native_termination(row)
        return
    failed_queries = []

    def participant_store_unavailable(execute, sql, params, many, context):
        if '"ctf_participant"."principal_uuid"' in sql:
            failed_queries.append(sql)
            raise OperationalError("Synthetic participation store unavailable")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(participant_store_unavailable):
        _assert_native_termination(row)
    # Both gateway renewal and the independent native cleanup cycle fail closed.
    assert len(failed_queries) == 2


def test_delivered_guacamole_terminates_after_owning_service_transfers_range(browser_remote):
    from cms.services import reassign_range_owner
    from management.services import create_service_principal
    from mission_control.guacamole_authority import renew_gateway_authority
    from workspaces.services import add_account_member

    actor, _, _ = browser_remote
    row = _deliver(browser_remote)
    assert renew_gateway_authority(str(row.pk))
    replacement = create_service_principal("Synthetic new remote owner")
    add_account_member(actor.account.uuid, replacement)
    reassign_range_owner(actor.instance.pk, replacement, credential=actor.credential, rehome=False)
    actor.instance.refresh_from_db()
    actor.realized.refresh_from_db()
    assert actor.instance.owner_principal_uuid == actor.realized.owner_principal_uuid == replacement.uuid
    _assert_native_termination(row)
