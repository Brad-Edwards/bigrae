"""Organizer recipients on the scoped communication ledger (ADR-065-R4, #528).

Drives the real alert declaration, the scheduled release task, the recipient
resolver and the inbox boundaries. Native policy is the test policy port from
``tests/ctf/conftest.py``; a test narrows or breaks it at the provider boundary.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.utils import timezone

from ctf.enums import EventStaffRole, ScheduledTaskType
from ctf.models import (
    CommunicationCampaign,
    CommunicationIntent,
    CTFEventStaff,
    CTFParticipant,
    CTFScheduledTask,
    DeliveryAttempt,
    RecipientReceipt,
    RecipientSnapshot,
)
from shared.auth import CTF_ORGANIZER_GROUP

pytestmark = pytest.mark.django_db


def _organizer(email: str) -> User:
    from management.services import get_user_profile

    user = User.objects.create_user(username=email, email=email, password="testpass123")  # nosec B106
    user.groups.add(Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)[0])
    profile = get_user_profile(user)
    profile.user_type = "ctf_organizer"
    profile.save(update_fields=["user_type"])
    return user


def _assign(owner, event, user, role):
    """Assign staff through the real staff API so native policy grants follow."""
    from django.test import Client

    from management.services import principal_for_user
    from tests.ctf._api_flow_helpers import call_json

    client = Client()
    client.force_login(owner)
    response = call_json(
        client,
        "post",
        "api_event_staff",
        kwargs={"event_id": event.id},
        body={"principal_uuid": str(principal_for_user(user).uuid), "role": role},
    )
    assert response.status_code == 201, response.content


@pytest.fixture
def staffed_event(ctf_event, organizer_user):
    """The owner, one live co-organizer and one moderator on one event."""
    co_organizer = _organizer("coorg@example.test")
    moderator = _organizer("moderator@example.test")
    _assign(organizer_user, ctf_event, co_organizer, EventStaffRole.CO_ORGANIZER.value)
    _assign(organizer_user, ctf_event, moderator, EventStaffRole.MODERATOR.value)
    return ctf_event, co_organizer, moderator


def _override_policy(monkeypatch, check):
    """Replace both provider entry points with ``check(request, original)``."""
    from shared.authorization import port

    original = port._provider_factory()
    provider = type(original)(
        **{
            **vars(original),
            "check": lambda request: check(request, original),
            "batch_check": lambda requests: tuple(check(request, original) for request in requests),
        }
    )
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)


def _declare(event, *, kind="event_started", action="event.read"):
    from ctf.services.notification import OrganizerAlert, declare_organizer_alert

    alert = OrganizerAlert(
        kind=kind,
        occurrence=event.event_start.isoformat(),
        subject="Event started automatically",
        body="The event started at its scheduled start time.",
        action=action,
    )
    with transaction.atomic():
        return declare_organizer_alert(event, alert)


def _run_release(result):
    from ctf.services.communication.scheduling import run_release_communication_task

    task = CTFScheduledTask.objects.get(
        task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value, metadata__intent_id=result["intent_id"]
    )
    return run_release_communication_task(task)


def _principal(user):
    from management.services import principal_for_user

    return principal_for_user(user).uuid


def test_declared_alert_releases_to_owner_and_co_organizers_only(staffed_event, organizer_user, settings):
    settings.EMAIL_DELIVERY_AVAILABLE = False
    event, co_organizer, moderator = staffed_event
    CTFParticipant.objects.create(
        event=event, email="player@example.test", name="Player", status="active", registered_at=timezone.now()
    )

    result = _declare(event)
    assert result["outcome"] == "declared"
    assert _run_release(result)["outcome"] == "released"

    snapshots = RecipientSnapshot.objects.filter(intent_id=result["intent_id"])
    assert {row.recipient_kind for row in snapshots} == {"organizer"}
    assert {row.principal_uuid for row in snapshots} == {_principal(organizer_user), _principal(co_organizer)}
    assert _principal(moderator) not in {row.principal_uuid for row in snapshots}
    assert {row.delivery_coordinate for row in snapshots} == {organizer_user.email, co_organizer.email}
    assert all(row.participant_public_id is None and row.participant_id is None for row in snapshots)
    assert RecipientReceipt.objects.filter(snapshot__in=snapshots).count() == 2
    # Email is unavailable in this deployment: only the in-app command is queued.
    assert set(DeliveryAttempt.objects.filter(snapshot__in=snapshots).values_list("channel", flat=True)) == {"in_app"}


def test_alert_identity_collapses_retries(staffed_event):
    event, *_ = staffed_event
    first = _declare(event)
    second = _declare(event)
    assert first == second
    assert CommunicationCampaign.objects.filter(pk=first["campaign_id"]).count() == 1
    assert CommunicationIntent.objects.filter(campaign_id=first["campaign_id"]).count() == 1
    _run_release(first)
    _run_release(first)
    assert RecipientSnapshot.objects.filter(intent_id=first["intent_id"]).count() == 2


def test_policy_denial_removes_a_candidate(staffed_event, organizer_user, monkeypatch):
    from shared.authorization import AuthorizationDecision, DecisionKind

    event, co_organizer, _ = staffed_event
    denied = _principal(co_organizer)

    def check(request, original):
        if request.principal.uuid == denied and request.action == "event.manage_ranges":
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return original.check(request)

    _override_policy(monkeypatch, check)
    result = _declare(event, kind="ranges_not_ready", action="event.manage_ranges")
    _run_release(result)

    principals = set(
        RecipientSnapshot.objects.filter(intent_id=result["intent_id"]).values_list("principal_uuid", flat=True)
    )
    assert principals == {_principal(organizer_user)}


def test_policy_outage_fails_closed_and_retries(staffed_event, monkeypatch):
    event, *_ = staffed_event
    result = _declare(event)

    def broken(request, original):
        raise RuntimeError("evaluator endpoint detail")

    _override_policy(monkeypatch, broken)
    outcome = _run_release(result)

    assert outcome["outcome"] == "backpressure_retry"
    assert not RecipientSnapshot.objects.filter(intent_id=result["intent_id"]).exists()
    assert CommunicationIntent.objects.get(pk=result["intent_id"]).status == "scheduled"


def test_revoked_co_organizer_is_not_a_recipient(staffed_event, organizer_user):
    event, co_organizer, _ = staffed_event
    CTFEventStaff.objects.get(event=event, principal_uuid=_principal(co_organizer)).delete(soft=True)
    # A stale policy grant without the live staff row is not enough.

    result = _declare(event)
    _run_release(result)

    principals = set(
        RecipientSnapshot.objects.filter(intent_id=result["intent_id"]).values_list("principal_uuid", flat=True)
    )
    assert principals == {_principal(organizer_user)}


def test_organizer_rows_never_reach_the_participant_inbox(staffed_event, organizer_user):
    from ctf.services.communication.inbox import inbox_for_participant

    event, *_ = staffed_event
    # The owner also plays in the event under the same account.
    CTFParticipant.objects.create(
        event=event,
        user=organizer_user,
        email=organizer_user.email,
        name="Owner",
        status="active",
        registered_at=timezone.now(),
    )
    result = _declare(event)
    _run_release(result)

    assert RecipientSnapshot.objects.filter(
        intent_id=result["intent_id"], principal_uuid=_principal(organizer_user)
    ).exists()
    assert not inbox_for_participant(organizer_user, event.pk).exists()


def test_public_authoring_cannot_select_organizers(ctf_event, organizer_user):
    from ctf.exceptions import CTFCommunicationError
    from ctf.services.communication import CampaignDraft, create_campaign
    from tests.ctf._account_scope import individual_account_uuid

    with pytest.raises(CTFCommunicationError):
        create_campaign(
            organizer_user,
            individual_account_uuid(organizer_user),
            CampaignDraft(
                title="Alert",
                origin="system_milestone",
                target_event_ids=[ctf_event.pk],
                audience_spec={"kind": "event_organizers", "event_ids": [str(ctf_event.pk)], "action": "event.read"},
                trigger_spec={"kind": "manual"},
                channels=["in_app"],
                subject="Alert",
                body="Alert",
            ),
            parent_type="account",
        )


def _revoke(owner, event, user):
    from django.test import Client

    from tests.ctf._api_flow_helpers import call_json

    client = Client()
    client.force_login(owner)
    response = call_json(
        client,
        "delete",
        "api_event_staff_member",
        kwargs={"event_id": event.id, "principal_uuid": _principal(user)},
    )
    assert response.status_code in (200, 204), response.content


def test_worker_suppresses_a_former_co_organizer(staffed_event, monkeypatch):
    from ctf.enums_communication import DeliveryStatus
    from ctf.services.communication import delivery as delivery_svc

    monkeypatch.setattr("ctf.services.communication.metrics._resolve_client", lambda: None)
    event, co_organizer, _ = staffed_event
    _run_release(_declare(event))
    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())
    former = next(row for row in claimed if row.snapshot.principal_uuid == _principal(co_organizer))

    # The staff row disappears after the claim, without the revoke lifecycle hook.
    CTFEventStaff.objects.filter(event=event, principal_uuid=_principal(co_organizer)).update(deleted_at=timezone.now())

    assert delivery_svc.process_attempt(former, cfg) == DeliveryStatus.SUPPRESSED.value
    assert DeliveryAttempt.objects.get(pk=former.pk).result_reason == "organizer_ineligible"


def test_staff_revocation_erases_the_coordinate_and_cancels_unclaimed_work(staffed_event, organizer_user):
    from ctf.enums_communication import DeliveryStatus

    event, co_organizer, _ = staffed_event
    result = _declare(event)
    _run_release(result)

    _revoke(organizer_user, event, co_organizer)

    revoked = RecipientSnapshot.objects.get(intent_id=result["intent_id"], principal_uuid=_principal(co_organizer))
    kept = RecipientSnapshot.objects.get(intent_id=result["intent_id"], principal_uuid=_principal(organizer_user))
    assert revoked.delivery_coordinate == ""
    assert kept.delivery_coordinate == organizer_user.email
    assert set(revoked.delivery_attempts.values_list("status", flat=True)) == {DeliveryStatus.CANCELLED.value}
    assert DeliveryStatus.CANCELLED.value not in set(kept.delivery_attempts.values_list("status", flat=True))


def test_demoted_co_organizer_coordinate_is_erased(staffed_event, organizer_user):
    event, co_organizer, _ = staffed_event
    result = _declare(event)
    _run_release(result)

    _assign(organizer_user, event, co_organizer, EventStaffRole.MODERATOR.value)

    demoted = RecipientSnapshot.objects.get(intent_id=result["intent_id"], principal_uuid=_principal(co_organizer))
    assert demoted.delivery_coordinate == ""


def test_communications_console_never_shows_organizer_alerts(staffed_event, organizer_user):
    from rest_framework.test import APIClient

    from tests.ctf._account_scope import individual_account_uuid

    event, *_ = staffed_event
    result = _declare(event)
    _run_release(result)
    client = APIClient()
    client.force_login(organizer_user)
    base = "/api/v1/ctf/communications/"

    listing = client.get(base, {"account": str(individual_account_uuid(organizer_user)), "event_id": str(event.pk)})
    assert listing.status_code == 200, listing.content
    assert result["campaign_id"] not in {item["id"] for item in listing.json()["results"]}
    for suffix in ("", "delivery/", "activity/"):
        assert client.get(f"{base}{result['campaign_id']}/{suffix}").status_code == 404
    cancel = client.post(f"{base}{result['campaign_id']}/cancel/", {}, format="json")
    assert cancel.status_code == 404
    assert CommunicationIntent.objects.get(pk=result["intent_id"]).status == "released"


def _alerts_url(event, suffix=""):
    return f"/api/v1/ctf/events/{event.pk}/organizer-alerts/{suffix}"


def _session(user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_login(user)
    return client


def test_organizer_inbox_lists_and_records_read_and_dismiss(staffed_event, organizer_user):
    event, co_organizer, _ = staffed_event
    result = _declare(event)
    _run_release(result)

    for user in (organizer_user, co_organizer):
        page = _session(user).get(_alerts_url(event))
        assert page.status_code == 200, page.content
        body = page.json()
        assert body["unread_count"] == 1
        assert [item["subject"] for item in body["results"]] == ["Event started automatically"]
        assert "delivery_coordinate" not in body["results"][0]
        assert page["Cache-Control"].startswith("max-age=0") or "no-cache" in page["Cache-Control"]

    client = _session(organizer_user)
    message_id = client.get(_alerts_url(event)).json()["results"][0]["message_id"]
    read = client.post(_alerts_url(event, f"{message_id}/read/"), {}, format="json")
    assert read.status_code == 200 and read.json()["read_at"]
    dismissed = client.post(_alerts_url(event, f"{message_id}/dismiss/"), {}, format="json")
    assert dismissed.status_code == 200 and dismissed.json()["dismissed_at"]
    assert client.get(_alerts_url(event)).json()["results"] == []
    assert len(client.get(_alerts_url(event), {"view": "dismissed"}).json()["results"]) == 1
    # Another organizer's receipt is untouched.
    assert _session(co_organizer).get(_alerts_url(event)).json()["unread_count"] == 1


def test_organizer_inbox_denies_non_organizers(staffed_event, participant_user):
    event, _, moderator = staffed_event
    _run_release(_declare(event))
    CTFParticipant.objects.create(
        event=event,
        user=participant_user,
        email=participant_user.email,
        name="Player",
        status="active",
        registered_at=timezone.now(),
    )
    for user in (moderator, participant_user):
        assert _session(user).get(_alerts_url(event)).status_code == 404


def test_organizer_inbox_hides_range_alerts_without_range_authority(staffed_event, co_organizer_denied_ranges):
    event, co_organizer, _ = staffed_event
    _run_release(_declare(event))
    co_organizer_denied_ranges(co_organizer)
    _run_release(_declare(event, kind="ranges_not_ready", action="event.manage_ranges"))

    subjects = [item["subject"] for item in _session(co_organizer).get(_alerts_url(event)).json()["results"]]
    assert subjects == ["Event started automatically"]


@pytest.fixture
def co_organizer_denied_ranges(monkeypatch):
    from shared.authorization import AuthorizationDecision, DecisionKind

    def deny(user):
        principal = _principal(user)

        def check(request, original):
            if request.principal.uuid == principal and request.action == "event.manage_ranges":
                return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
            return original.check(request)

        _override_policy(monkeypatch, check)

    return deny


def test_revoked_co_organizer_loses_the_inbox(staffed_event, organizer_user):
    event, co_organizer, _ = staffed_event
    _run_release(_declare(event))
    _revoke(organizer_user, event, co_organizer)

    assert _session(co_organizer).get(_alerts_url(event)).status_code == 404


def test_organizer_inbox_rejects_tokens_and_requires_csrf(staffed_event, organizer_user):
    from rest_framework.test import APIClient

    from shared.api_tokens.models import ApiToken

    event, *_ = staffed_event
    result = _declare(event)
    _run_release(result)
    _token, raw = ApiToken.create_token(name="alerts", created_by=organizer_user, scopes=["ctf:communication:read"])
    token_client = APIClient()
    token_client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert token_client.get(_alerts_url(event)).status_code in (401, 403)

    csrf_client = APIClient(enforce_csrf_checks=True)
    csrf_client.force_login(organizer_user)
    message_id = _session(organizer_user).get(_alerts_url(event)).json()["results"][0]["message_id"]
    assert csrf_client.post(_alerts_url(event, f"{message_id}/read/"), {}, format="json").status_code == 403


def test_worker_rechecks_native_policy_before_organizer_delivery(staffed_event, monkeypatch):
    from ctf.enums_communication import DeliveryStatus
    from ctf.services.communication import delivery as delivery_svc
    from shared.authorization import AuthorizationDecision, DecisionKind

    monkeypatch.setattr("ctf.services.communication.metrics._resolve_client", lambda: None)
    event, co_organizer, _ = staffed_event
    _run_release(_declare(event, kind="ranges_not_ready", action="event.manage_ranges"))
    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())
    denied_row = next(row for row in claimed if row.snapshot.principal_uuid == _principal(co_organizer))
    denied = _principal(co_organizer)

    def check(request, original):
        if request.principal.uuid == denied and request.action == "event.manage_ranges":
            return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")
        return original.check(request)

    # Range authority is denied after release, while the staff row stays live.
    _override_policy(monkeypatch, check)
    assert delivery_svc.process_attempt(denied_row, cfg) == DeliveryStatus.SUPPRESSED.value
    assert DeliveryAttempt.objects.get(pk=denied_row.pk).result_reason == "organizer_policy_denied"


def test_worker_keeps_organizer_work_retriable_during_a_policy_outage(staffed_event, monkeypatch):
    from ctf.enums_communication import DeliveryStatus
    from ctf.services.communication import delivery as delivery_svc

    monkeypatch.setattr("ctf.services.communication.metrics._resolve_client", lambda: None)
    event, *_ = staffed_event
    _run_release(_declare(event))
    cfg = delivery_svc.WorkerConfig.from_settings()
    row = delivery_svc.claim_batch(cfg, now=timezone.now())[0]

    def broken(request, original):
        raise RuntimeError("evaluator endpoint detail")

    _override_policy(monkeypatch, broken)
    assert delivery_svc.process_attempt(row, cfg) == DeliveryStatus.RETRY_DUE.value
    attempt = DeliveryAttempt.objects.get(pk=row.pk)
    assert attempt.result_reason == "authority_unavailable"
    assert attempt.attempted_at is None
