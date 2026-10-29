"""Email channel on the communication ledger (ADR-051-R13, ADR-071, #1525).

Drives real admission, the real worker, the real adapter, and the real
``shared.email`` seam. Only the external mail transport is replaced: Django's
locmem backend, or a fake backend raising a real SMTP exception (ADR-019-R1).
"""

from __future__ import annotations

import smtplib

import pytest
from django.core.mail.backends.base import BaseEmailBackend
from django.db.models import TextField, Value
from django.utils import timezone

from ctf.enums import ParticipantStatus
from ctf.enums_communication import DeliveryStatus, IntentDeliveryState
from ctf.models import CommunicationIntent, CTFParticipant, DeliveryAttempt, RecipientSnapshot
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from ctf.services.communication import delivery as delivery_svc
from ctf.services.communication.adapters import get_adapter, registered_channels
from shared import email as shared_email
from tests.ctf._account_scope import individual_account_uuid

pytestmark = pytest.mark.django_db

REFUSED = "refused@test.com"


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
