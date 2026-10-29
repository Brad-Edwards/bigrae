"""Delivery engine: worker claim/lease/fence/retry, in-app channel, and metrics.

Covers issue #2098 (CTF-008): the lease-based delivery worker consuming the #2048
``DeliveryAttempt`` outbox, the in-app channel as a durable inbox with a
reference-only WebSocket wake-up on a stable identity, and fail-soft metrics. These
are SQLite-lane unit tests (the coverage publisher); real-boundary concurrency and
recovery live in ``test_communication_delivery_postgres.py``.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

import workspaces.services as workspace_services
from ctf.enums import ParticipantStatus
from ctf.enums_communication import DeliveryStatus, IntentDeliveryState
from ctf.models import CommunicationIntent, CTFParticipant, DeliveryAttempt, ParticipantReceipt, RecipientSnapshot
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from ctf.services.communication import delivery as delivery_svc
from ctf.services.communication.adapters import contract as adapter_contract
from ctf.services.communication.adapters import register_adapter
from ctf.services.communication.adapters.contract import DeliveryOutcome, OutcomeClass
from ctf.services.notification.realtime import publish_communication_wakeup
from shared.models import WebSocketNotification

pytestmark = pytest.mark.django_db

User = get_user_model()


class _NullPublisher:
    """A put_metric_data-compatible sink so metrics emit runs real logic off the cloud."""

    def put_metric_data(self, **kwargs: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _hermetic_metrics(monkeypatch):
    """Keep the worker's metrics emission off the real cloud (ADR-019: mock the transport)."""
    monkeypatch.setattr("ctf.services.communication.metrics._resolve_client", lambda: _NullPublisher())


def _workspace_uuid(user):
    return str(workspace_services.resolve_personal_workspace(user).workspace_uuid)


def _participant(event, email, *, user=None, status=ParticipantStatus.ACTIVE.value):
    return CTFParticipant.objects.create(
        event=event,
        user=user,
        email=email,
        name=email.split("@")[0],
        status=status,
        registered_at=timezone.now(),
    )


def _draft(ctf_event, *, channels=("in_app", "email"), **overrides) -> CampaignDraft:
    data = {
        "title": "Kickoff",
        "origin": "organizer_staff",
        "target_event_ids": [ctf_event.id],
        "audience_spec": {"kind": "event", "event_ids": [str(ctf_event.id)]},
        "trigger_spec": {"kind": "manual"},
        "channels": list(channels),
        "subject": "Welcome",
        "body": "Read the rules at [rules](/events/rules).",
    }
    data.update(overrides)
    return CampaignDraft(**data)


def _release(organizer_user, ctf_event, *, channels=("in_app", "email"), occurrence="occ-1"):
    campaign = create_campaign(organizer_user, _workspace_uuid(organizer_user), _draft(ctf_event, channels=channels))
    return release_campaign(campaign, occurrence_key=occurrence, actor_user_id=organizer_user.id)


# ---------------------------------------------------------------------------
# In-app availability is committed at admission, independent of the worker.
# ---------------------------------------------------------------------------


def test_release_commits_in_app_availability_without_a_worker(organizer_user, ctf_event):
    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["in_app"])

    # The durable inbox entry (receipt) exists at admission, before any worker runs.
    assert ParticipantReceipt.objects.filter(snapshot__intent=intent).count() == 1


def test_email_only_release_creates_no_in_app_receipt(organizer_user, ctf_event):
    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["email"])

    # Receipt existence must not expose an email-only item as an in-app inbox entry.
    assert ParticipantReceipt.objects.filter(snapshot__intent=intent).count() == 0
    assert DeliveryAttempt.objects.filter(intent=intent, channel="email").count() == 1


# ---------------------------------------------------------------------------
# Worker: in-app delivery outcomes.
# ---------------------------------------------------------------------------


def test_worker_accepts_account_less_in_app_delivery(organizer_user, ctf_event):
    _participant(ctf_event, "a@test.com")  # no linked account -> nothing to wake
    _release(organizer_user, ctf_event, channels=["in_app"])

    stats = delivery_svc.run_once()

    assert stats.claimed == 1
    assert stats.accepted == 1
    attempt = DeliveryAttempt.objects.get(channel="in_app")
    assert attempt.status == DeliveryStatus.ACCEPTED.value
    assert attempt.result_reason == "no_socket_recipient"
    assert attempt.lease_token == ""
    assert attempt.observed_at is not None


def test_worker_publishes_wakeup_on_a_stable_identity(organizer_user, ctf_event, settings):
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = True
    account = User.objects.create_user(username="p@test.com", email="p@test.com", password="x")  # nosec B106
    _participant(ctf_event, "p@test.com", user=account)
    _release(organizer_user, ctf_event, channels=["in_app"])

    stats = delivery_svc.run_once()

    assert stats.accepted == 1
    snapshot = RecipientSnapshot.objects.get()
    notifications = WebSocketNotification.objects.filter(recipient_id=account.id)
    # The wake-up uses the stable snapshot id as the replay identity, never the event UUID.
    assert notifications.count() == 1
    assert str(notifications.get().event_id) == str(snapshot.id)


def test_worker_does_not_claim_email_while_the_channel_is_unavailable(organizer_user, ctf_event, settings):
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])
    settings.EMAIL_DELIVERY_AVAILABLE = False

    stats = delivery_svc.run_once()

    # An unavailable channel is never claimed, downgraded, or accepted.
    assert stats.claimed == 0
    assert DeliveryAttempt.objects.get(channel="email").status == DeliveryStatus.QUEUED.value


def test_wakeup_replay_is_idempotent(organizer_user, ctf_event, settings):
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = True
    account = User.objects.create_user(username="p@test.com", email="p@test.com", password="x")  # nosec B106
    _participant(ctf_event, "p@test.com", user=account)
    intent = _release(organizer_user, ctf_event, channels=["in_app"])
    snapshot = RecipientSnapshot.objects.get(intent=intent)
    refs = {"snapshot_id": str(snapshot.id), "intent_id": str(intent.id)}

    publish_communication_wakeup(
        event_id=ctf_event.id, recipient_user_id=account.id, snapshot_id=snapshot.id, references=refs
    )
    publish_communication_wakeup(
        event_id=ctf_event.id, recipient_user_id=account.id, snapshot_id=snapshot.id, references=refs
    )

    # A replayed wake-up maps to the same row: reconnect never duplicates the entry.
    assert WebSocketNotification.objects.filter(recipient_id=account.id).count() == 1


# ---------------------------------------------------------------------------
# Worker: fencing, staleness, retry/expiry, stale-lease recovery.
# ---------------------------------------------------------------------------


def test_worker_suppresses_delivery_when_intent_is_fenced_after_claim(organizer_user, ctf_event):
    from ctf.enums_communication import IntentStatus

    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["in_app"])
    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())
    assert len(claimed) == 1

    # A cancellation/fence lands after the claim; the worker suppresses before I/O.
    intent.status = IntentStatus.FENCED.value
    intent.save(update_fields=["status", "updated_at"])
    result = delivery_svc.process_attempt(claimed[0], cfg)

    assert result == DeliveryStatus.SUPPRESSED.value
    attempt = DeliveryAttempt.objects.get(pk=claimed[0].pk)
    assert attempt.status == DeliveryStatus.SUPPRESSED.value
    assert attempt.result_reason == "intent_fenced"


def test_worker_suppresses_when_participant_becomes_ineligible_after_claim(organizer_user, ctf_event):
    participant = _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["in_app"])
    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())

    # The participant is banned (removed from viewing eligibility) after the claim.
    CTFParticipant.objects.filter(pk=participant.pk).update(status="banned")
    result = delivery_svc.process_attempt(claimed[0], cfg)

    assert result == DeliveryStatus.SUPPRESSED.value
    attempt = DeliveryAttempt.objects.get(pk=claimed[0].pk)
    assert attempt.status == DeliveryStatus.SUPPRESSED.value
    assert attempt.result_reason == "participant_ineligible"


def test_stale_worker_cannot_settle_a_reclaimed_command(organizer_user, ctf_event):
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["in_app"])
    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())
    stale = claimed[0]  # holds the old lease token in memory

    # Simulate another worker reclaiming the lease (new token).
    DeliveryAttempt.objects.filter(pk=stale.pk).update(lease_token="different-token")
    result = delivery_svc.process_attempt(stale, cfg)

    assert result == "stale"
    # The row keeps the reclaiming worker's lease, untouched by the stale worker.
    assert DeliveryAttempt.objects.get(pk=stale.pk).lease_token == "different-token"


def test_retriable_failure_expires_when_attempt_budget_is_exhausted(organizer_user, ctf_event, settings):
    # notifications disabled -> the in-app wake-up is "unavailable" -> retriable.
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = False
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 1
    account = User.objects.create_user(username="p@test.com", email="p@test.com", password="x")  # nosec B106
    _participant(ctf_event, "p@test.com", user=account)
    _release(organizer_user, ctf_event, channels=["in_app"])

    stats = delivery_svc.run_once()

    # attempt_number reaches the ceiling of 1 on the first try -> EXPIRED, not endless retry.
    assert stats.expired == 1
    assert DeliveryAttempt.objects.get(channel="in_app").status == DeliveryStatus.EXPIRED.value


def test_retriable_failure_schedules_a_backoff_retry(organizer_user, ctf_event, settings):
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = False
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 5
    account = User.objects.create_user(username="p@test.com", email="p@test.com", password="x")  # nosec B106
    _participant(ctf_event, "p@test.com", user=account)
    _release(organizer_user, ctf_event, channels=["in_app"])

    delivery_svc.run_once()

    attempt = DeliveryAttempt.objects.get(channel="in_app")
    assert attempt.status == DeliveryStatus.RETRY_DUE.value
    assert attempt.due_at > timezone.now()
    assert attempt.lease_token == ""


def test_claim_recovers_a_stale_lease(organizer_user, ctf_event):
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["in_app"])
    attempt = DeliveryAttempt.objects.get(channel="in_app")
    # Simulate a crashed worker: CLAIMED with an expired lease and an old token.
    DeliveryAttempt.objects.filter(pk=attempt.pk).update(
        status=DeliveryStatus.CLAIMED.value,
        lease_token="dead-worker",
        lease_expires_at=timezone.now() - timedelta(minutes=5),
        attempt_number=1,
    )

    cfg = delivery_svc.WorkerConfig.from_settings()
    claimed = delivery_svc.claim_batch(cfg, now=timezone.now())

    assert len(claimed) == 1
    reclaimed = DeliveryAttempt.objects.get(pk=attempt.pk)
    assert reclaimed.lease_token not in ("", "dead-worker")  # a fresh lease fences the dead worker
    assert reclaimed.attempt_number == 2  # crash/reclaim consumes the attempt budget too


# ---------------------------------------------------------------------------
# Channel adapter outcomes: a registered adapter's terminal/suppressed result.
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_email_adapter():
    """Replace the email adapter with a controllable fake; restore the real one after.

    The fake returns a scripted outcome and counts calls, so tests exercise the
    worker's settle paths without a transport.
    """
    box = {"outcome": DeliveryOutcome(OutcomeClass.TERMINAL, reason="hard_bounce"), "calls": 0}

    class _FakeEmailAdapter:
        channel = "email"

        def available(self):
            return True

        def deliver(self, command, *, timeout):
            box["calls"] += 1
            return box["outcome"]

    real = adapter_contract._ADAPTERS.get("email")
    register_adapter(_FakeEmailAdapter())
    try:
        yield box
    finally:
        adapter_contract._ADAPTERS.pop("email", None)
        if real is not None:
            register_adapter(real)


def test_worker_records_a_terminal_channel_failure(organizer_user, ctf_event, registered_email_adapter):
    registered_email_adapter["outcome"] = DeliveryOutcome(OutcomeClass.TERMINAL, reason="hard_bounce")
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])

    stats = delivery_svc.run_once()

    assert stats.failed == 1
    attempt = DeliveryAttempt.objects.get(channel="email")
    assert attempt.status == DeliveryStatus.PERMANENT_FAILURE.value
    assert attempt.result_reason == "hard_bounce"


def test_worker_records_an_adapter_suppression(organizer_user, ctf_event, registered_email_adapter):
    registered_email_adapter["outcome"] = DeliveryOutcome(OutcomeClass.SUPPRESSED, reason="opted_out")
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])

    stats = delivery_svc.run_once()

    assert stats.suppressed == 1
    assert DeliveryAttempt.objects.get(channel="email").status == DeliveryStatus.SUPPRESSED.value


def test_worker_retries_when_the_adapter_raises(organizer_user, ctf_event, registered_email_adapter, settings):
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 5

    class _Boom:
        channel = "email"

        def available(self):
            return True

        def deliver(self, command, *, timeout):
            raise RuntimeError("provider exploded")

    register_adapter(_Boom())
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])

    stats = delivery_svc.run_once()

    # A raising adapter is caught and treated as retriable, never aborting the batch.
    assert stats.retried == 1
    assert DeliveryAttempt.objects.get(channel="email").status == DeliveryStatus.RETRY_DUE.value


def test_drain_command_processes_one_batch(organizer_user, ctf_event, capsys):
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["in_app"])

    call_command("drain_ctf_communication_deliveries")

    assert DeliveryAttempt.objects.get(channel="in_app").status == DeliveryStatus.ACCEPTED.value
    assert "claimed=1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Retry budget before I/O, graceful shutdown, and recovery counters (#1525).
# ---------------------------------------------------------------------------


def test_spent_attempt_budget_expires_before_transport_io(
    organizer_user, ctf_event, registered_email_adapter, settings
):
    settings.CTF_COMMUNICATION_MAX_ATTEMPTS = 2
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])
    # A crash loop already spent both attempts; the lease then expired.
    DeliveryAttempt.objects.update(
        status=DeliveryStatus.CLAIMED.value,
        lease_token="dead-worker",
        lease_expires_at=timezone.now() - timedelta(minutes=1),
        attempt_number=2,
        first_attempt_at=timezone.now() - timedelta(minutes=10),
    )

    stats = delivery_svc.run_once()

    attempt = DeliveryAttempt.objects.get()
    assert registered_email_adapter["calls"] == 0
    assert (attempt.status, attempt.result_reason) == (DeliveryStatus.EXPIRED.value, "budget_exhausted")
    assert (stats.expired, stats.reclaimed) == (1, 1)


def test_spent_elapsed_budget_expires_a_due_retry_before_transport_io(
    organizer_user, ctf_event, registered_email_adapter, settings
):
    settings.CTF_COMMUNICATION_MAX_ELAPSED_SECONDS = 60
    _participant(ctf_event, "a@test.com")
    _release(organizer_user, ctf_event, channels=["email"])
    DeliveryAttempt.objects.update(
        status=DeliveryStatus.RETRY_DUE.value,
        attempt_number=1,
        first_attempt_at=timezone.now() - timedelta(minutes=5),
        due_at=timezone.now(),
    )

    delivery_svc.run_once()

    assert registered_email_adapter["calls"] == 0
    assert DeliveryAttempt.objects.get().status == DeliveryStatus.EXPIRED.value


def test_shutdown_releases_unattempted_claims_with_their_budget_restored(
    organizer_user, ctf_event, registered_email_adapter
):
    registered_email_adapter["outcome"] = DeliveryOutcome(OutcomeClass.ACCEPTED, reason="backend_accepted")
    for index in range(3):
        _participant(ctf_event, f"p{index}@test.com")
    _release(organizer_user, ctf_event, channels=["email"])

    stats = delivery_svc.run_once(should_stop=lambda: registered_email_adapter["calls"] >= 1)

    assert registered_email_adapter["calls"] == 1
    assert (stats.claimed, stats.accepted, stats.released) == (3, 1, 2)
    released = DeliveryAttempt.objects.exclude(status=DeliveryStatus.ACCEPTED.value)
    assert released.count() == 2
    for attempt in released:
        assert attempt.status == DeliveryStatus.QUEUED.value
        assert attempt.attempt_number == 0
        assert attempt.lease_token == ""
        assert attempt.first_attempt_at is None


def test_settlement_writes_one_bounded_log_line(organizer_user, ctf_event, registered_email_adapter, caplog):
    import logging

    _participant(ctf_event, "secret-recipient@test.com")
    _release(organizer_user, ctf_event, channels=["email"])

    # The "ctf" logger does not propagate to the root handler; attach caplog directly.
    worker_logger = logging.getLogger("ctf.services.communication.delivery")
    worker_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="ctf.services.communication.delivery"):
            delivery_svc.run_once()
    finally:
        worker_logger.removeHandler(caplog.handler)

    lines = [record.getMessage() for record in caplog.records if "settled" in record.getMessage()]
    assert len(lines) == 1
    assert "channel=email" in lines[0] and "status=permanent_failure" in lines[0] and "reason=hard_bounce" in lines[0]
    assert "secret-recipient" not in "\n".join(record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# Persisted occurrence delivery state (#1525).
# ---------------------------------------------------------------------------


def _state(intent) -> str:
    return CommunicationIntent.objects.get(pk=intent.pk).delivery_state


def test_in_app_only_release_is_accepted_even_when_the_wakeup_must_retry(organizer_user, ctf_event, settings):
    settings.WEBSOCKET_NOTIFICATIONS_ENABLED = False
    account = User.objects.create_user(username="w@test.com", email="w@test.com", password="x")  # nosec B106
    _participant(ctf_event, "w@test.com", user=account)
    intent = _release(organizer_user, ctf_event, channels=["in_app"])

    assert _state(intent) == IntentDeliveryState.ACCEPTED.value
    delivery_svc.run_once()

    assert DeliveryAttempt.objects.get(channel="in_app").status == DeliveryStatus.RETRY_DUE.value
    assert _state(intent) == IntentDeliveryState.ACCEPTED.value


def test_email_settlement_moves_the_occurrence_from_pending_to_failed(
    organizer_user, ctf_event, registered_email_adapter
):
    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["email"])
    assert _state(intent) == IntentDeliveryState.PENDING.value

    delivery_svc.run_once()

    assert _state(intent) == IntentDeliveryState.FAILED.value


def test_cancelling_unclaimed_work_withdraws_the_occurrence(organizer_user, ctf_event):
    from ctf.services.communication import AdmissionActor, cancel_campaign

    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["email"])

    cancel_campaign(intent.campaign, actor=AdmissionActor(user_id=organizer_user.id))

    assert _state(intent) == IntentDeliveryState.WITHDRAWN.value


def test_release_without_recipients_is_empty(organizer_user, ctf_event):
    intent = _release(organizer_user, ctf_event, channels=["email"])

    assert _state(intent) == IntentDeliveryState.EMPTY.value


def test_worker_reconciles_a_settlement_whose_state_refresh_was_lost(
    organizer_user, ctf_event, registered_email_adapter
):
    _participant(ctf_event, "a@test.com")
    intent = _release(organizer_user, ctf_event, channels=["email"])
    # A crash between the settle commit and the refresh leaves the state behind.
    DeliveryAttempt.objects.update(status=DeliveryStatus.ACCEPTED.value, result_reason="backend_accepted")
    assert _state(intent) == IntentDeliveryState.PENDING.value

    delivery_svc.run_once()

    assert registered_email_adapter["calls"] == 0
    assert _state(intent) == IntentDeliveryState.ACCEPTED.value


def test_occurrence_stays_pending_until_its_last_email_command_settles(
    organizer_user, ctf_event, registered_email_adapter
):
    registered_email_adapter["outcome"] = DeliveryOutcome(OutcomeClass.ACCEPTED, reason="backend_accepted")
    _participant(ctf_event, "a@test.com")
    _participant(ctf_event, "b@test.com")
    intent = _release(organizer_user, ctf_event, channels=["email"])
    cfg = delivery_svc.WorkerConfig.from_settings()
    first, second = delivery_svc.claim_batch(cfg, now=timezone.now())

    delivery_svc.process_attempt(first, cfg)
    assert _state(intent) == IntentDeliveryState.PENDING.value

    delivery_svc.process_attempt(second, cfg)
    assert _state(intent) == IntentDeliveryState.ACCEPTED.value
