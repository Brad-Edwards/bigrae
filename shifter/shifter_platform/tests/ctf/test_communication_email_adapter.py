"""Email channel on the communication ledger (ADR-051-R14, ADR-071, #1525).

Drives real admission, the real worker, the real adapter, and the real
``shared.email`` seam. Only the external mail transport is replaced: Django's
locmem backend, or a fake backend raising a real SMTP exception (ADR-019-R1).
"""

from __future__ import annotations

import smtplib
from zoneinfo import ZoneInfo

import pytest
from django.core.mail.backends.base import BaseEmailBackend
from django.db.models import TextField, Value
from django.utils import timezone

from ctf.enums import ParticipantStatus
from ctf.enums_communication import DeliveryStatus, IntentDeliveryState
from ctf.models import CommunicationIntent, CTFEvent, CTFParticipant, DeliveryAttempt, RecipientSnapshot
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from ctf.services.communication import delivery as delivery_svc
from ctf.services.communication.adapters import get_adapter, registered_channels
from shared import email as shared_email
from tests.ctf._account_scope import individual_account_uuid
from tests.ctf.conftest import individual_event_scope

pytestmark = pytest.mark.django_db

REFUSED = "refused@test.com"
SITE_URL = "https://ctf.example.test"


class _NullPublisher:
    def put_metric_data(self, **kwargs: object) -> None:
        return None


class RefusingBackend(BaseEmailBackend):
    """External SMTP stand-in: refuses one address, accepts every other one."""

    code = 550

    def send_messages(self, email_messages):
        message = email_messages[0]
        if message.to == [REFUSED]:
            raise smtplib.SMTPRecipientsRefused({REFUSED: (RefusingBackend.code, b"mailbox unavailable")})
        message.message()
        return 1


@pytest.fixture(autouse=True)
def _hermetic_metrics(monkeypatch):
    monkeypatch.setattr("ctf.services.communication.metrics._resolve_client", lambda: _NullPublisher())


@pytest.fixture(autouse=True)
def _site_url(settings):
    settings.SITE_URL = SITE_URL


def _participant(event, address):
    return CTFParticipant.objects.create(
        event=event,
        email=address,
        name=address.split("@")[0] or "nameless",
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
    )


def _release(owner, event, *, channels=("email",), subject="Welcome", body="Read the [rules](/rules)."):
    draft = CampaignDraft(
        title="Kickoff",
        origin="organizer_staff",
        target_event_ids=[event.id],
        audience_spec={"kind": "event", "event_ids": [str(event.id)]},
        trigger_spec={"kind": "manual"},
        channels=list(channels),
        subject=subject,
        body=body,
    )
    campaign = create_campaign(owner, individual_account_uuid(owner), draft, parent_type="account")
    return release_campaign(campaign, occurrence_key="occ-1", actor_user_id=owner.id)


def test_email_is_registered_only_while_a_delivering_backend_exists(settings):
    assert "email" in registered_channels()
    assert get_adapter("email") is not None

    settings.EMAIL_DELIVERY_AVAILABLE = False

    assert "email" not in registered_channels()
    assert get_adapter("email") is None
    assert "in_app" in registered_channels()


def test_release_queues_work_without_transport_io(organizer_user, ctf_event, mailoutbox):
    _participant(ctf_event, "a@test.com")

    intent = _release(organizer_user, ctf_event)

    assert mailoutbox == []
    assert DeliveryAttempt.objects.get(intent=intent).status == DeliveryStatus.QUEUED.value
    assert CommunicationIntent.objects.get(pk=intent.pk).delivery_state == IntentDeliveryState.PENDING.value


def test_worker_sends_one_escaped_message_per_recipient_with_a_stable_message_id(
    organizer_user, ctf_event, mailoutbox, settings
):
    settings.DEFAULT_FROM_EMAIL = "Events <events@mail.example.test>"
    _participant(ctf_event, "a@test.com")
    _participant(ctf_event, "b@test.com")
    intent = _release(organizer_user, ctf_event, subject="Line one\nLine two", body="Score < 10 & rank > 2")

    stats = delivery_svc.run_once()

    assert stats.accepted == 2
    assert sorted(message.to[0] for message in mailoutbox) == ["a@test.com", "b@test.com"]
    for message in mailoutbox:
        assert len(message.to) == 1 and not message.cc and not message.bcc
        assert message.subject == "Line one Line two"
        html = message.alternatives[0][0]
        assert "Score &lt; 10 &amp; rank &gt; 2" in html
        assert "Score < 10 & rank > 2" in message.body
        attempt = DeliveryAttempt.objects.get(intent=intent, snapshot__participant__email=message.to[0])
        assert message.extra_headers["Message-ID"] == shared_email._message_id(
            attempt.idempotency_key, settings.DEFAULT_FROM_EMAIL
        )
        assert attempt.status == DeliveryStatus.ACCEPTED.value
        assert attempt.result_reason == "backend_accepted"


def test_retry_of_one_command_reuses_the_same_message_id(organizer_user, ctf_event, mailoutbox, settings):
    settings.EMAIL_BACKEND = "tests.ctf.test_communication_email_adapter.FlakyBackend"
    FlakyBackend.failures = 1
    FlakyBackend.message_ids = []
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event)

    delivery_svc.run_once()
    DeliveryAttempt.objects.update(due_at=timezone.now())
    delivery_svc.run_once()

    attempt = DeliveryAttempt.objects.get()
    assert attempt.status == DeliveryStatus.ACCEPTED.value
    assert attempt.attempt_number == 2
    assert len(set(FlakyBackend.message_ids)) == 1


class FlakyBackend(BaseEmailBackend):
    """External transport that fails transiently a set number of times."""

    failures = 0
    message_ids: list[str] = []

    def send_messages(self, email_messages):
        FlakyBackend.message_ids.append(email_messages[0].extra_headers["Message-ID"])
        if FlakyBackend.failures:
            FlakyBackend.failures -= 1
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return 1


def test_erased_coordinate_fails_only_its_command(organizer_user, ctf_event, mailoutbox):
    _participant(ctf_event, "a@test.com")
    _participant(ctf_event, "b@test.com")
    intent = _release(organizer_user, ctf_event)
    RecipientSnapshot.objects.filter(intent=intent, participant__email="a@test.com").update(delivery_coordinate="")

    delivery_svc.run_once()

    statuses = dict(DeliveryAttempt.objects.values_list("result_reason", "status"))
    assert statuses == {
        "coordinate_unavailable": DeliveryStatus.PERMANENT_FAILURE.value,
        "backend_accepted": DeliveryStatus.ACCEPTED.value,
    }
    assert [message.to for message in mailoutbox] == [["b@test.com"]]


def test_unreadable_coordinate_is_retried_without_poisoning_the_batch(organizer_user, ctf_event, mailoutbox, settings):
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 3
    _participant(ctf_event, "a@test.com")
    _participant(ctf_event, "b@test.com")
    intent = _release(organizer_user, ctf_event)
    poisoned = RecipientSnapshot.objects.get(intent=intent, participant__email="a@test.com")
    # Store an undecryptable token as a rotated key or corruption would; a plain
    # text expression bypasses the encrypting field's own value preparation.
    RecipientSnapshot.objects.filter(pk=poisoned.pk).update(
        delivery_coordinate=Value("enc:v1:not-a-token", output_field=TextField())
    )

    delivery_svc.run_once()

    poisoned_attempt = DeliveryAttempt.objects.get(snapshot=poisoned)
    assert (poisoned_attempt.status, poisoned_attempt.result_reason) == (
        DeliveryStatus.RETRY_DUE.value,
        "coordinate_unreadable",
    )
    assert [message.to for message in mailoutbox] == [["b@test.com"]]


def test_refused_recipient_is_terminal_and_the_occurrence_is_partial(organizer_user, ctf_event, settings):
    settings.EMAIL_BACKEND = "tests.ctf.test_communication_email_adapter.RefusingBackend"
    _participant(ctf_event, REFUSED)
    _participant(ctf_event, "ok@test.com")
    intent = _release(organizer_user, ctf_event)

    stats = delivery_svc.run_once()

    assert (stats.accepted, stats.failed) == (1, 1)
    refused = DeliveryAttempt.objects.get(snapshot__participant__email=REFUSED)
    assert (refused.status, refused.result_reason) == (DeliveryStatus.PERMANENT_FAILURE.value, "recipient_rejected")
    intent.refresh_from_db()
    assert intent.delivery_state == IntentDeliveryState.PARTIAL.value
    assert intent.delivery_state_changed_at is not None


def test_temporary_recipient_refusal_is_retried_by_the_worker(organizer_user, ctf_event, settings):
    settings.EMAIL_BACKEND = "tests.ctf.test_communication_email_adapter.RefusingBackend"
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 5
    RefusingBackend.code = 451
    try:
        _participant(ctf_event, REFUSED)
        intent = _release(organizer_user, ctf_event)

        stats = delivery_svc.run_once()
    finally:
        RefusingBackend.code = 550

    attempt = DeliveryAttempt.objects.get()
    assert stats.retried == 1
    assert (attempt.status, attempt.result_reason) == (DeliveryStatus.RETRY_DUE.value, "recipient_deferred")
    assert CommunicationIntent.objects.get(pk=intent.pk).delivery_state == IntentDeliveryState.PENDING.value


def test_worker_transport_timeout_reaches_the_backend(organizer_user, ctf_event, settings, monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(shared_email, "connection_options", lambda backend, timeout: seen.append(timeout) or {})
    settings.CTF_COMMUNICATION_TRANSPORT_TIMEOUT_SECONDS = 7
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event)

    delivery_svc.run_once()

    assert seen == [7.0]


# --- Notice templates (#580) -------------------------------------------------


def test_organizer_announcement_renders_its_default_pair_with_snapshot_event_facts(
    organizer_user, ctf_event, mailoutbox
):
    ctf_event.name = "Spring <Cup> & Co"
    ctf_event.event_timezone = "America/New_York"
    ctf_event.save(update_fields=["name", "event_timezone"])
    _participant(ctf_event, "ada@test.com")
    intent = _release(organizer_user, ctf_event, subject="Kickoff", body="Doors open at 9 & seats < 50.")

    delivery_svc.run_once()

    assert intent.campaign.notice_kind == "announcement"
    (message,) = mailoutbox
    html = message.alternatives[0][0]
    portal = f"{SITE_URL}/ctf/event/"
    assert "Spring &lt;Cup&gt; &amp; Co" in html
    assert "Doors open at 9 &amp; seats &lt; 50." in html
    assert f'href="{portal}"' in html
    assert "Spring <Cup> & Co" in message.body
    assert "Hi ada," in message.body
    assert "Doors open at 9 & seats < 50." in message.body
    assert portal in message.body


@pytest.mark.parametrize(
    "sender,marker",
    [
        ("send_reminder", "starts on"),
        ("send_cleanup_warning", "range cleanup"),
        ("send_event_results", "final results"),
    ],
)
def test_each_event_notice_kind_renders_its_own_default_pair(ctf_event, mailoutbox, sender, marker):
    from ctf.models import CommunicationCampaign
    from ctf.services import notification

    ctf_event.event_timezone = "Asia/Tokyo"
    ctf_event.save(update_fields=["event_timezone"])
    _participant(ctf_event, "ada@test.com")

    staged = getattr(notification, sender)(ctf_event.pk)
    delivery_svc.run_once()

    assert staged["outcome"] == "accepted"
    kind = CommunicationCampaign.objects.get(pk=staged["campaign_id"]).notice_kind
    assert (
        kind
        == {"send_reminder": "reminder", "send_cleanup_warning": "cleanup", "send_event_results": "results"}[sender]
    )
    (message,) = mailoutbox
    assert marker in message.body.lower()
    assert marker in message.alternatives[0][0].lower()
    if sender == "send_reminder":
        local_start = timezone.localtime(ctf_event.event_start, ZoneInfo("Asia/Tokyo"))
        assert f"{local_start:%Y-%m-%d %H:%M} (Asia/Tokyo)" in message.body


@pytest.mark.parametrize(
    "sender,kind,marker",
    [
        ("send_range_ready", "range_ready", "range is ready"),
        ("notify_participant_provision_failure", "range_failure", "could not prepare"),
    ],
)
def test_each_participant_notice_kind_renders_its_own_default_pair(ctf_event, mailoutbox, sender, kind, marker):
    from ctf.models import CommunicationCampaign
    from ctf.services import notification

    participant = _participant(ctf_event, "ada@test.com")

    staged = getattr(notification, sender)(participant.pk)
    delivery_svc.run_once()

    assert CommunicationCampaign.objects.get(pk=staged["campaign_id"]).notice_kind == kind
    (message,) = mailoutbox
    assert marker in message.body.lower()
    assert marker in message.alternatives[0][0].lower()


def test_every_notice_kind_has_a_trusted_template_pair():
    from django.template.loader import get_template

    from ctf.enums_communication import NoticeKind

    for kind in NoticeKind:
        get_template(f"ctf/email/notice/{kind.value}.html")
        get_template(f"ctf/email/notice/{kind.value}.txt")


def test_multi_event_campaign_uses_each_recipients_own_event(organizer_user, ctf_event, mailoutbox):
    second = CTFEvent.objects.create(
        name="Second Event",
        created_by=organizer_user,
        **individual_event_scope(organizer_user),
        status=ctf_event.status,
        event_start=ctf_event.event_start,
        event_end=ctf_event.event_end,
        scenario_id="basic",
    )
    _participant(ctf_event, "first@test.com")
    _participant(second, "second@test.com")
    draft = CampaignDraft(
        title="Kickoff",
        origin="organizer_staff",
        target_event_ids=[ctf_event.id, second.id],
        audience_spec={"kind": "multi_event", "event_ids": [str(ctf_event.id), str(second.id)]},
        trigger_spec={"kind": "manual"},
        channels=["email"],
        subject="Welcome",
        body="Hello",
    )
    campaign = create_campaign(organizer_user, individual_account_uuid(organizer_user), draft, parent_type="account")
    release_campaign(campaign, occurrence_key="occ-1", actor_user_id=organizer_user.id)

    delivery_svc.run_once()

    by_address = {message.to[0]: message.body for message in mailoutbox}
    assert ctf_event.name in by_address["first@test.com"] and "Second Event" not in by_address["first@test.com"]
    assert "Second Event" in by_address["second@test.com"] and ctf_event.name not in by_address["second@test.com"]


def test_unsafe_site_url_is_a_retriable_dependency_fault(organizer_user, ctf_event, mailoutbox, settings):
    settings.SITE_URL = "http://attacker.example/path"
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event)

    delivery_svc.run_once()

    attempt = DeliveryAttempt.objects.get()
    assert (attempt.status, attempt.result_reason) == (DeliveryStatus.RETRY_DUE.value, "site_url_unavailable")
    assert mailoutbox == []


def test_organizer_authoring_cannot_select_a_system_notice_kind(organizer_user, ctf_event):
    from ctf.exceptions import CTFCommunicationError

    draft = CampaignDraft(
        title="Forged",
        origin="organizer_staff",
        target_event_ids=[ctf_event.id],
        audience_spec={"kind": "event", "event_ids": [str(ctf_event.id)]},
        trigger_spec={"kind": "manual"},
        channels=["email"],
        subject="Range ready",
        body="Hello",
        notice_kind="range_ready",
    )

    with pytest.raises(CTFCommunicationError) as exc:
        create_campaign(organizer_user, individual_account_uuid(organizer_user), draft, parent_type="account")

    assert exc.value.code == "CTF_COMMUNICATION_NOTICE_KIND_DENIED"


def test_database_rejects_an_unknown_notice_kind(organizer_user, ctf_event):
    from django.db import IntegrityError, transaction

    from ctf.models import CommunicationCampaign

    intent = _release(organizer_user, ctf_event)

    with pytest.raises(IntegrityError), transaction.atomic():
        CommunicationCampaign.objects.filter(pk=intent.campaign_id).update(notice_kind="invite")


def test_persisted_notice_kind_cannot_change_to_another_valid_kind(organizer_user, ctf_event):
    from django.core.exceptions import ValidationError

    from ctf.models import CommunicationCampaign

    campaign = CommunicationCampaign.objects.get(pk=_release(organizer_user, ctf_event).campaign_id)
    campaign.notice_kind = "range_ready"

    with pytest.raises(ValidationError) as exc:
        campaign.save()

    assert "notice_kind" in exc.value.message_dict
    assert CommunicationCampaign.objects.get(pk=campaign.pk).notice_kind == "announcement"
