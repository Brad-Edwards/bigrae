"""REST authorization and closed-envelope behavior for scoped communications."""

import pytest
from rest_framework.test import APIClient

from ctf.models import CommunicationCampaign, CommunicationIntent
from shared.api_tokens.models import ApiToken
from shared.api_tokens.scopes import AUTHORIZATION_ACTION_SCOPES
from shared.principal_port import principal_for_user
from workspaces.models import Account

pytestmark = pytest.mark.django_db
URL = "/api/v1/ctf/communications/"


def test_scheduler_history_cannot_bypass_communication_scope_or_leak_errors(organizer_user, ctf_event):
    from django.utils import timezone

    from ctf.models import CTFScheduledTask

    task = CTFScheduledTask.objects.create(
        event=ctf_event,
        task_type="release_communication",
        scheduled_for=timezone.now(),
        error_message="private-error-canary",
        metadata={"intent_id": "missing"},
    )
    client, _ = token_client(
        organizer_user,
        ["ctf:event:read", AUTHORIZATION_ACTION_SCOPES["event.manage_lifecycle"]],
    )
    response = client.get(f"/api/v1/ctf/events/{ctf_event.pk}/tasks/")
    assert response.status_code == 200
    assert str(task.pk) not in response.content.decode()
    assert "private-error-canary" not in response.content.decode()


def body(event, owner):
    principal = principal_for_user(owner)
    account, _ = Account.objects.get_or_create(
        individual_principal_uuid=principal.uuid,
        defaults={"kind": Account.Kind.INDIVIDUAL, "name": f"Test account {owner.pk}"},
    )

    return {
        "account": str(account.uuid),
        "title": "Welcome",
        "target_event_ids": [str(event.pk)],
        "audience_spec": {"kind": "event", "event_ids": [str(event.pk)]},
        "trigger_spec": {"kind": "manual"},
        "channels": ["in_app"],
        "subject": "Welcome",
        "body": "Read the rules",
    }


def token_client(owner, scopes):
    token, raw = ApiToken.create_token(name="test", created_by=owner, scopes=scopes)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client, token


def test_token_can_create_release_and_cancel(organizer_user, ctf_event):
    client, _ = token_client(organizer_user, ["ctf:communication:write", "ctf:communication:read"])
    response = client.post(URL, body(ctf_event, organizer_user), format="json")
    assert response.status_code == 201, response.content
    identity = response.json()["id"]
    detail = client.get(f"{URL}{identity}/")
    assert detail.status_code == 200, detail.content
    assert detail.json()["id"] == identity
    listing = client.get(URL, {"account": body(ctf_event, organizer_user)["account"]})
    assert listing.status_code == 200, listing.content
    assert [item["id"] for item in listing.json()["results"]] == [identity]
    response = client.post(f"{URL}{identity}/release/", {"occurrence_key": "release-1"}, format="json")
    assert response.status_code == 202, response.content
    assert response.json()["status"] == "released"
    retry = client.post(f"{URL}{identity}/release/", {"occurrence_key": "release-1"}, format="json")
    assert retry.json()["id"] == response.json()["id"]
    assert CommunicationIntent.objects.count() == 1
    assert client.post(f"{URL}{identity}/cancel/", {}, format="json").status_code == 200


def test_personal_read_scope_cannot_write_through_the_communication_service(organizer_user, ctf_event):
    from ctf.exceptions import CTFCommunicationError
    from ctf.services.communication import AdmissionActor, CampaignDraft, create_campaign
    from shared.api_tokens.policy import personal_credential_for_token

    _client, token = token_client(organizer_user, ["ctf:communication:read"])
    credential = personal_credential_for_token(token)
    data = body(ctf_event, organizer_user)
    account_uuid = data.pop("account")
    with pytest.raises(CTFCommunicationError) as denied:
        create_campaign(
            credential,
            account_uuid,
            CampaignDraft(**data, origin="organizer_staff"),
            actor=AdmissionActor(credential=credential),
            parent_type="account",
        )
    assert denied.value.code == "CTF_COMMUNICATION_TOKEN_DENIED"
    assert not CommunicationCampaign.objects.exists()


@pytest.mark.parametrize("scopes", [["ctf:event:write"], ["ctf:communication:read"]])
def test_wrong_write_scope_is_denied(organizer_user, ctf_event, scopes):
    client, _ = token_client(organizer_user, scopes)
    response = client.post(URL, body(ctf_event, organizer_user), format="json")
    assert response.status_code == 403
    assert not CommunicationCampaign.objects.exists()


def test_invalid_bearer_does_not_fall_back_to_session(organizer_user, ctf_event):
    client = APIClient()
    client.force_login(organizer_user)
    client.credentials(HTTP_AUTHORIZATION="Bearer shf_invalid.invalid")
    assert client.post(URL, body(ctf_event, organizer_user), format="json").status_code == 401


def test_unknown_fields_and_malformed_values_do_not_leak(organizer_user, ctf_event, caplog):
    client, _ = token_client(organizer_user, ["ctf:communication:write"])
    data = body(ctf_event, organizer_user)
    data["private-canary"] = "private-value"
    response = client.post(URL, data, format="json")
    assert response.status_code == 400
    assert "error" in response.json()
    assert "private-canary" not in response.content.decode() + caplog.text
    assert "private-value" not in response.content.decode() + caplog.text


def test_multi_event_campaign_is_not_disclosed_to_one_event_owner(
    organizer_user,
    second_organizer_user,
    organizer_credential,
    ctf_event,
    live_session_credential,
):
    from ctf.enums import EventStaffRole
    from ctf.models import CTFEvent
    from ctf.services.communication import CampaignDraft, create_campaign
    from ctf.services.event import assign_event_staff, revoke_event_staff, transfer_event_ownership

    second = CTFEvent.objects.create(
        name="Second",
        created_by=organizer_user,
        scope_kind=ctf_event.scope_kind,
        account_id=ctf_event.account_id,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
    )
    campaign = create_campaign(
        organizer_user,
        body(ctf_event, organizer_user)["account"],
        CampaignDraft(
            title="private-campaign",
            origin="organizer_staff",
            target_event_ids=[ctf_event.pk, second.pk],
            audience_spec={"kind": "multi_event", "event_ids": [str(ctf_event.pk), str(second.pk)]},
            trigger_spec={"kind": "manual"},
            channels=["in_app"],
            subject="Secret",
            body="Secret",
        ),
        parent_type="account",
    )
    new_owner = principal_for_user(second_organizer_user)
    assign_event_staff(
        second.pk,
        organizer_credential,
        new_owner.uuid,
        EventStaffRole.CO_ORGANIZER.value,
    )
    transfer_event_ownership(second.pk, organizer_credential, new_owner.uuid)
    revoke_event_staff(
        second.pk,
        live_session_credential(second_organizer_user),
        principal_for_user(organizer_user).uuid,
    )
    client, _ = token_client(organizer_user, ["ctf:communication:read"])
    assert client.get(f"{URL}{campaign.pk}/").status_code == 404
    response = client.get(URL, {"account": body(ctf_event, organizer_user)["account"]})
    assert response.status_code == 200
    assert "private-campaign" not in response.content.decode()
    visible = create_campaign(
        organizer_user,
        body(ctf_event, organizer_user)["account"],
        CampaignDraft(
            title="Visible",
            origin="organizer_staff",
            target_event_ids=[ctf_event.pk],
            audience_spec={"kind": "event", "event_ids": [str(ctf_event.pk)]},
            trigger_spec={"kind": "manual"},
            channels=["in_app"],
            subject="Visible",
            body="Visible",
        ),
        parent_type="account",
    )
    for event_filter in ({}, {"event_id": str(ctf_event.pk)}):
        response = client.get(
            URL,
            {
                "account": body(ctf_event, organizer_user)["account"],
                "limit": 1,
                **event_filter,
            },
        )
        assert [item["id"] for item in response.json()["results"]] == [str(visible.pk)]
        assert response.json()["next_offset"] is None


def test_authoring_rate_limit_bounds_retained_drafts(organizer_user, ctf_event, settings):
    from django.core.cache import cache

    cache.clear()
    settings.CTF_COMMUNICATION_RATE_PER_ACTOR = 1
    client, _ = token_client(organizer_user, ["ctf:communication:write"])
    assert client.post(URL, body(ctf_event, organizer_user), format="json").status_code == 201
    response = client.post(URL, body(ctf_event, organizer_user), format="json")
    assert response.status_code == 429
    assert response["Retry-After"]
    assert CommunicationCampaign.objects.count() == 1


def test_unavailable_email_channel_is_not_accepted_for_delivery(organizer_user, ctf_event):
    client, _ = token_client(organizer_user, ["ctf:communication:write"])
    data = body(ctf_event, organizer_user)
    data["channels"] = ["email"]
    response = client.post(URL, data, format="json")
    assert response.status_code == 201
    response = client.post(f"{URL}{response.json()['id']}/release/", {"occurrence_key": "email-1"}, format="json")
    assert response.status_code == 503
    assert not CommunicationIntent.objects.exists()


def test_deleted_target_does_not_open_campaign_visibility(organizer_user, ctf_event):
    from django.utils import timezone

    from ctf.models import CTFEvent

    client, _ = token_client(organizer_user, ["ctf:communication:write", "ctf:communication:read"])
    response = client.post(URL, body(ctf_event, organizer_user), format="json")
    CTFEvent.all_objects.filter(pk=ctf_event.pk).update(deleted_at=timezone.now())
    assert client.get(f"{URL}{response.json()['id']}/").status_code == 404


def test_collection_excludes_deleted_target_before_paging(organizer_user, ctf_event, ctf_event_draft):
    from django.utils import timezone

    from ctf.models import CTFEvent

    client, _token = token_client(organizer_user, ["ctf:communication:write", "ctf:communication:read"])
    hidden = client.post(URL, body(ctf_event, organizer_user), format="json")
    visible = client.post(URL, body(ctf_event_draft, organizer_user), format="json")
    assert hidden.status_code == visible.status_code == 201
    CTFEvent.all_objects.filter(pk=ctf_event.pk).update(deleted_at=timezone.now())
    response = client.get(URL, {"account": body(ctf_event_draft, organizer_user)["account"], "limit": 1})
    assert response.status_code == 200, response.content
    assert [row["id"] for row in response.json()["results"]] == [visible.json()["id"]]
    assert response.json()["next_offset"] is None


@pytest.mark.parametrize("failure", ["revoked", "outage", "evaluator_error"])
def test_collection_preserves_credential_and_provider_failures(organizer_user, ctf_event, monkeypatch, failure):
    from shared.authorization import (
        AuthorizationDecision,
        AuthorizationProviderError,
        DecisionKind,
        configured_authorization_provider,
    )

    client, token = token_client(organizer_user, ["ctf:communication:write", "ctf:communication:read"])
    assert client.post(URL, body(ctf_event, organizer_user), format="json").status_code == 201
    provider = configured_authorization_provider()
    check = provider.check

    def unavailable(request):
        if request.action == "event.manage_communications":
            if failure == "outage":
                raise AuthorizationProviderError("private-collection-canary")
            return AuthorizationDecision(DecisionKind.EVALUATOR_ERROR, "evaluator_unavailable")
        return check(request)

    if failure == "revoked":
        token.revoke()
    else:
        monkeypatch.setattr(provider, "check", unavailable)
    response = client.get(URL, {"account": body(ctf_event, organizer_user)["account"]})
    assert response.status_code == (401 if failure == "revoked" else 503), response.content
    assert "private-collection-canary" not in response.content.decode()


@pytest.mark.parametrize("role", ["moderator", "judge", "co_organizer"])
def test_collection_authority_query_matches_live_staff_resolver(ctf_event, second_organizer_user, role):
    from django.utils import timezone

    from ctf.models import CTFEventStaff
    from ctf.services.authorization import events_with_capability, resolve_event_authority

    staff = CTFEventStaff.objects.create(event=ctf_event, user=second_organizer_user, role=role)

    def visible():
        return (
            events_with_capability(second_organizer_user, capability="notifications").filter(pk=ctf_event.pk).exists()
        )

    assert visible() == (
        resolve_event_authority(second_organizer_user, ctf_event, capability="notifications") is not None
    )
    CTFEventStaff.all_objects.filter(pk=staff.pk).update(deleted_at=timezone.now())
    assert not visible()
    assert resolve_event_authority(second_organizer_user, ctf_event, capability="notifications") is None
