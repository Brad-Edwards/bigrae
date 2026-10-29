"""Session-only, parent-scoped durable inbox and explicit receipt actions."""

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from ctf.models import CTFParticipant, ParticipantReceipt
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from shared.api_tokens.models import ApiToken
from tests.ctf._account_scope import individual_account_uuid

pytestmark = pytest.mark.django_db


@pytest.fixture
def inbox(organizer_user, ctf_event, django_user_model):
    user = django_user_model.objects.create_user(username="recipient")
    participant = CTFParticipant.objects.create(
        event=ctf_event,
        user=user,
        email="recipient@example.test",
        name="Recipient",
        status="active",
        registered_at=timezone.now(),
    )
    campaign = create_campaign(
        organizer_user,
        individual_account_uuid(organizer_user),
        CampaignDraft(
            title="Notice",
            origin="organizer_staff",
            target_event_ids=[ctf_event.pk],
            audience_spec={"kind": "event", "event_ids": [str(ctf_event.pk)]},
            trigger_spec={"kind": "manual"},
            channels=["in_app"],
            subject="Notice",
            body="Read this",
            acknowledgement_policy="explicit",
        ),
        parent_type="account",
    )
    intent = release_campaign(campaign, occurrence_key="inbox-1", actor_user_id=organizer_user.pk)
    snapshot = intent.recipient_snapshots.get()
    client = APIClient()
    client.force_login(user)
    url = f"/api/v1/ctf/me/events/{ctf_event.pk}/communications/"
    return client, user, participant, snapshot, url


def test_get_is_read_only_and_explicit_receipts_are_idempotent(inbox):
    client, _, _, snapshot, url = inbox
    response = client.get(url)
    assert response.status_code == 200, response.content
    assert response.json()["results"][0]["message_id"] == str(snapshot.pk)
    assert client.get(f"{url}{snapshot.pk}/").status_code == 200
    receipt = ParticipantReceipt.objects.get(snapshot=snapshot)
    assert receipt.read_at is None and receipt.acknowledged_at is None
    for action, field in [("read", "read_at"), ("acknowledge", "acknowledged_at")]:
        response = client.post(f"{url}{snapshot.pk}/{action}/", {}, format="json")
        assert response.status_code == 200, response.content
        receipt.refresh_from_db()
        first = getattr(receipt, field)
        assert first is not None
        assert client.post(f"{url}{snapshot.pk}/{action}/", {}, format="json").status_code == 200
        receipt.refresh_from_db()
        assert getattr(receipt, field) == first


def test_token_is_never_a_receipt_session(inbox):
    client, user, _, snapshot, url = inbox
    _, raw = ApiToken.create_token(
        name="read", created_by=user, scopes=["ctf:communication:read", "ctf:communication:write"]
    )
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert client.get(url).status_code == 403
    assert client.post(f"{url}{snapshot.pk}/read/", {}, format="json").status_code == 403


def test_removed_or_other_parent_cannot_read_or_ack(inbox, django_user_model):
    from uuid import uuid4

    client, _, participant, snapshot, url = inbox
    assert client.get(f"/api/v1/ctf/me/events/{uuid4()}/communications/{snapshot.pk}/").status_code == 404
    participant.status = "banned"
    participant.save()
    assert client.get(url).status_code == 404
    assert client.post(f"{url}{snapshot.pk}/acknowledge/", {}, format="json").status_code == 404


def test_receipt_write_requires_csrf(inbox):
    _, user, _, snapshot, url = inbox
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(f"{url}{snapshot.pk}/read/", {}, format="json").status_code == 403


def _release(organizer, event, *, policy="none", key, title="Notice", body="Read this"):
    campaign = create_campaign(
        organizer,
        individual_account_uuid(organizer),
        CampaignDraft(
            title=title,
            origin="organizer_staff",
            target_event_ids=[event.pk],
            audience_spec={"kind": "event", "event_ids": [str(event.pk)]},
            trigger_spec={"kind": "manual"},
            channels=["in_app"],
            subject=title,
            body=body,
            acknowledgement_policy=policy,
        ),
        parent_type="account",
    )
    return campaign, release_campaign(campaign, occurrence_key=key, actor_user_id=organizer.pk)


def _ids(response):
    assert response.status_code == 200, response.content
    return [item["message_id"] for item in response.json()["results"]]


def _second_event(organizer):
    from datetime import timedelta

    from ctf.enums import EventStatus
    from ctf.models import CTFEvent
    from tests.ctf._account_scope import individual_account_scope_fields

    return CTFEvent.objects.create(
        name="Second CTF Event",
        created_by=organizer,
        **individual_account_scope_fields(organizer),
        status=EventStatus.REGISTRATION.value,
        event_start=timezone.now() + timedelta(days=2),
        event_end=timezone.now() + timedelta(days=2, hours=8),
        scenario_id="basic",
    )


def test_dismiss_read_and_acknowledge_are_independent_first_write_facts(inbox):
    client, _, _, snapshot, url = inbox
    item_url = f"{url}{snapshot.pk}/"
    receipt = ParticipantReceipt.objects.get(snapshot=snapshot)

    dismissed = client.post(f"{item_url}dismiss/", {}, format="json")
    assert dismissed.status_code == 200, dismissed.content
    body = dismissed.json()
    assert body["dismissed_at"] is not None and body["read_at"] is None and body["acknowledged_at"] is None
    assert body["action_required"] is True
    receipt.refresh_from_db()
    first = receipt.dismissed_at
    assert client.post(f"{item_url}dismiss/", {}, format="json").status_code == 200
    assert client.post(f"{item_url}read/", {}, format="json").status_code == 200
    receipt.refresh_from_db()
    assert receipt.dismissed_at == first and receipt.read_at is not None and receipt.acknowledged_at is None

    acknowledged = client.post(f"{item_url}acknowledge/", {}, format="json").json()
    assert acknowledged["action_required"] is False
    assert acknowledged["dismissed_at"] == body["dismissed_at"]


def test_views_and_counts_keep_open_obligations_reachable(inbox, organizer_user, ctf_event):
    client, _, _, explicit, url = inbox
    _, plain = _release(organizer_user, ctf_event, key="plain")
    _, needs_read = _release(organizer_user, ctf_event, policy="read", key="needs-read")
    plain_id, read_id, explicit_id = (
        str(plain.recipient_snapshots.get().pk),
        str(needs_read.recipient_snapshots.get().pk),
        str(explicit.pk),
    )

    page = client.get(url).json()
    assert (page["unread_count"], page["action_required_count"]) == (3, 2)
    assert set(_ids(client.get(f"{url}?view=action_required"))) == {explicit_id, read_id}

    client.post(f"{url}{plain_id}/dismiss/", {}, format="json")
    client.post(f"{url}{explicit_id}/dismiss/", {}, format="json")
    # A dismissed message with an open obligation stays in the default view.
    assert set(_ids(client.get(url))) == {explicit_id, read_id}
    assert set(_ids(client.get(f"{url}?view=dismissed"))) == {plain_id, explicit_id}

    client.post(f"{url}{explicit_id}/acknowledge/", {}, format="json")
    client.post(f"{url}{read_id}/read/", {}, format="json")
    page = client.get(url).json()
    assert [item["message_id"] for item in page["results"]] == [read_id]
    assert (page["unread_count"], page["action_required_count"]) == (0, 0)
    assert _ids(client.get(f"{url}?view=action_required")) == []

    paged = client.get(f"{url}?view=dismissed&limit=1").json()
    assert len(paged["results"]) == 1 and paged["next_offset"] == 1


def test_inbox_is_bound_to_the_live_participation_for_each_event(inbox, organizer_user, django_user_model):
    client, user, participant, first_snapshot, url = inbox
    other_user = django_user_model.objects.create_user(username="other-recipient")
    CTFParticipant.objects.create(
        event=participant.event,
        user=other_user,
        email="other@example.test",
        name="Other",
        status="active",
        registered_at=timezone.now(),
    )
    _, other_intent = _release(organizer_user, participant.event, key="for-both")
    mine = other_intent.recipient_snapshots.get(user_id=user.pk)
    theirs = other_intent.recipient_snapshots.get(user_id=other_user.pk)
    assert str(theirs.pk) not in _ids(client.get(url))
    assert client.get(f"{url}{theirs.pk}/").status_code == 404
    assert client.post(f"{url}{theirs.pk}/read/", {}, format="json").status_code == 404

    # The same account later joins another event: each event's inbox shows only
    # that participation's messages, and an ended participation keeps nothing.
    second = _second_event(organizer_user)
    participant.delete()
    CTFParticipant.objects.create(
        event=second,
        user=user,
        email="recipient@example.test",
        name="Recipient",
        status="active",
        registered_at=timezone.now(),
    )
    _, second_intent = _release(organizer_user, second, key="second-event")
    second_url = f"/api/v1/ctf/me/events/{second.pk}/communications/"
    assert _ids(client.get(second_url)) == [str(second_intent.recipient_snapshots.get().pk)]
    for snapshot in (first_snapshot, mine):
        assert client.get(f"{second_url}{snapshot.pk}/").status_code == 404
        assert client.post(f"{second_url}{snapshot.pk}/dismiss/", {}, format="json").status_code == 404
    assert client.get(url).status_code == 404


def test_distinct_occurrences_stay_distinct_and_retries_converge(inbox, organizer_user, ctf_event):
    client, _, _, _, url = inbox
    campaign, first = _release(organizer_user, ctf_event, key="round-1", title="Same text")
    replay = release_campaign(campaign, occurrence_key="round-1", actor_user_id=organizer_user.pk)
    second = release_campaign(campaign, occurrence_key="round-2", actor_user_id=organizer_user.pk)
    assert replay.pk == first.pk
    ids = _ids(client.get(url))
    assert len(ids) == len(set(ids)) == 3
    assert {str(first.recipient_snapshots.get().pk), str(second.recipient_snapshots.get().pk)} <= set(ids)


def test_retention_cut_off_hides_messages_before_purge(inbox, settings):
    from datetime import timedelta

    client, _, participant, snapshot, url = inbox
    event = participant.event
    event.event_start = timezone.now() - timedelta(days=settings.CTF_COMMUNICATION_RETENTION_DAYS + 3)
    event.event_end = timezone.now() - timedelta(days=settings.CTF_COMMUNICATION_RETENTION_DAYS + 2)
    event.save(update_fields=["event_start", "event_end"])
    assert _ids(client.get(url)) == []
    assert client.get(f"{url}{snapshot.pk}/").status_code == 404
    assert client.post(f"{url}{snapshot.pk}/read/", {}, format="json").status_code == 404
    assert ParticipantReceipt.objects.filter(snapshot=snapshot).exists()


def test_inbox_responses_are_private_and_never_repeat_input(inbox, settings):
    client, _, _, snapshot, url = inbox
    settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS = frozenset({"docs.example.com", "cdn.example.com"})
    listed = client.get(url)
    assert listed.json()["allowed_link_hosts"] == ["cdn.example.com", "docs.example.com"]
    probe = "<script>alert(1)</script>"
    responses = [
        listed,
        client.get(f"{url}{snapshot.pk}/"),
        client.post(f"{url}{snapshot.pk}/read/", {}, format="json"),
        client.get(url, {"view": probe}),
        client.get(url, {"limit": probe}),
        client.get(url, {probe: "1"}),
        client.post(f"{url}{snapshot.pk}/dismiss/", {probe: probe}, format="json"),
    ]
    assert [r.status_code for r in responses] == [200, 200, 200, 400, 400, 400, 400]
    for response in responses:
        directives = {d.strip() for d in response["Cache-Control"].split(",")}
        assert {"no-store", "private"} <= directives
        assert "script" not in response.content.decode()
