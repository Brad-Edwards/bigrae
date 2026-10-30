"""Automatic start/end, late ranges and the organizer alerts they declare (#528).

CTF-1002 and CTF-1004: the scheduler handlers run against real rows. Each alert
is asserted as a durable declaration (campaign, scheduled intent and release
task) that commits with the state change, not as a mocked callback.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from ctf.enums import EventStatus, ScheduledTaskStatus, ScheduledTaskType
from ctf.exceptions import CTFStateError
from ctf.models import CommunicationCampaign, CommunicationIntent, CTFEvent, CTFParticipant, CTFScheduledTask

pytestmark = pytest.mark.django_db


def _move(event, **times):
    CTFEvent.objects.filter(pk=event.pk).update(**times)
    event.refresh_from_db()
    return event


def _task(event, task_type, when=None):
    return CTFScheduledTask.objects.create(
        event=event,
        task_type=task_type,
        scheduled_for=when or timezone.now(),
        status=ScheduledTaskStatus.PENDING.value,
    )


def _alerts(event):
    """Return {alert subject prefix: campaign} for the event's organizer alerts."""
    campaigns = CommunicationCampaign.objects.filter(
        target_events=event, audience_spec__kind="event_organizers"
    ).order_by("created_at")
    return {campaign.title.split(":")[0]: campaign for campaign in campaigns}


def _body(campaign):
    return campaign.message_revisions.get().body


def _due_start(ctf_event):
    return _move(ctf_event, event_start=timezone.now() - timedelta(minutes=1))


@pytest.fixture
def content_not_ready(settings, ctf_event):
    """Configure managed content for the event's scenario without a hydration receipt."""
    import json

    from shared.schemas.ctf_content_reference import load_ctf_content_references_json

    original = settings.CTF_CONTENT_REFERENCES
    reference = {"scenario_id": ctf_event.scenario_id, "object_key": "ctf/content-bundles/aa/bundle.json"}
    settings.CTF_CONTENT_REFERENCES = load_ctf_content_references_json(
        json.dumps(
            {
                "contract": "shifter-ctf-content-references/v1",
                "references": [{**reference, "digest": f"sha256:{'a' * 64}"}],
            }
        ),
        prefix="ctf/content-bundles",
    )
    return original


def test_automatic_start_activates_and_declares_the_alert(ctf_event):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_start

    event = _due_start(ctf_event)
    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))

    event.refresh_from_db()
    assert event.status == EventStatus.ACTIVE.value
    started = _alerts(event)["Event started automatically"]
    assert started.audience_spec["action"] == "event.read"
    intent = CommunicationIntent.objects.get(campaign=started)
    assert intent.status == "scheduled"
    assert CTFScheduledTask.objects.filter(
        task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value, metadata__intent_id=str(intent.pk)
    ).exists()


def test_start_alert_commits_with_the_transition(ctf_event):
    from ctf.services.event import start_event_automatically

    event = _due_start(ctf_event)
    with pytest.raises(RuntimeError), transaction.atomic():
        start_event_automatically(event.pk)
        raise RuntimeError("crash before commit")

    event.refresh_from_db()
    assert event.status == EventStatus.REGISTRATION.value
    assert _alerts(event) == {}


def test_early_start_task_reschedules_without_side_effects(ctf_event):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_start

    result = _handle_event_start(_task(ctf_event, ScheduledTaskType.EVENT_START.value))

    assert result["reschedule_for"] == ctf_event.event_start
    ctf_event.refresh_from_db()
    assert ctf_event.status == EventStatus.REGISTRATION.value
    assert _alerts(ctf_event) == {}


def test_refused_start_retries_and_alerts_once(ctf_event, content_not_ready):
    from ctf.management.commands.run_ctf_scheduler import AutomaticStartRefused, _handle_event_start

    event = _due_start(ctf_event)
    for _ in range(2):
        with pytest.raises(AutomaticStartRefused):
            _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))

    event.refresh_from_db()
    assert event.status == EventStatus.REGISTRATION.value
    alerts = _alerts(event)
    assert "Event started automatically" not in alerts
    refused = alerts["Event could not start automatically"]
    assert CommunicationIntent.objects.filter(campaign=refused).count() == 1


def _participant(event, name, *, status="active", range_status="", range_instance_id=None):
    return CTFParticipant.objects.create(
        event=event,
        email=f"{name}@example.test",
        name=name,
        status=status,
        registered_at=timezone.now(),
        range_status=range_status,
        range_instance_id=range_instance_id,
    )


def test_ranges_not_ready_at_start_alert_carries_cohort_counts(ctf_event):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_start

    event = _due_start(ctf_event)
    _participant(event, "ready", range_status="ready", range_instance_id=11)
    _participant(event, "building", range_status="provisioning", range_instance_id=12)
    _participant(event, "unassigned")
    # Cached ready without an assignment is not readiness.
    _participant(event, "stale", range_status="ready")
    # A banned participant is outside the registered cohort.
    _participant(event, "banned", status="banned", range_status="error")

    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))
    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))

    late = _alerts(event)["Participant ranges are not ready"]
    assert late.audience_spec["action"] == "event.manage_ranges"
    assert "1 of 4 participant ranges were ready" in _body(late)
    assert "3 were not ready" in _body(late)
    assert "@example.test" not in _body(late)
    assert CommunicationIntent.objects.filter(campaign=late).count() == 1


def test_no_late_alert_when_every_range_is_ready_or_cohort_is_empty(ctf_event):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_start

    event = _due_start(ctf_event)
    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))
    assert "Participant ranges are not ready" not in _alerts(event)

    _participant(event, "ready", range_status="ready", range_instance_id=11)
    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))
    assert "Participant ranges are not ready" not in _alerts(event)


def test_late_alert_is_checked_even_when_start_is_refused(ctf_event, content_not_ready):
    from ctf.management.commands.run_ctf_scheduler import AutomaticStartRefused, _handle_event_start

    event = _due_start(ctf_event)
    _participant(event, "building", range_status="provisioning", range_instance_id=12)
    with pytest.raises(AutomaticStartRefused):
        _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))

    assert "Participant ranges are not ready" in _alerts(event)


@pytest.mark.parametrize("status", [EventStatus.ACTIVE.value, EventStatus.PAUSED.value])
def test_automatic_end_ends_active_or_paused_events_and_alerts(ctf_event_active, status):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_end

    event = _move(ctf_event_active, status=status, event_end=timezone.now() - timedelta(minutes=1))
    _handle_event_end(_task(event, ScheduledTaskType.EVENT_END.value, event.event_end))

    event.refresh_from_db()
    assert event.status == EventStatus.ENDED.value
    assert "Event ended automatically" in _alerts(event)


def test_end_alert_commits_with_the_transition(ctf_event_active):
    from ctf.services.event import complete_event

    event = _move(ctf_event_active, event_end=timezone.now() - timedelta(minutes=1))
    with pytest.raises(RuntimeError), transaction.atomic():
        complete_event(event, automatic=True)
        raise RuntimeError("crash before commit")

    event.refresh_from_db()
    assert event.status == EventStatus.ACTIVE.value
    assert _alerts(event) == {}


def test_end_replay_reapplies_idempotent_end_effects(ctf_event_active):
    from ctf.management.commands.run_ctf_scheduler import _handle_event_end

    event = _move(ctf_event_active, status=EventStatus.ENDED.value, event_end=timezone.now() - timedelta(minutes=1))
    _handle_event_end(_task(event, ScheduledTaskType.EVENT_END.value, event.event_end))
    _handle_event_end(_task(event, ScheduledTaskType.EVENT_END.value, event.event_end))

    # The idempotent results notice is staged once; no automatic-end alert is invented.
    results = CommunicationCampaign.objects.filter(target_events=event, title="Event results available")
    assert results.count() == 1
    assert _alerts(event) == {}


def test_manual_end_sends_no_automatic_alert(ctf_event_active):
    from ctf.services.event import complete_event

    assert complete_event(ctf_event_active) is True
    assert _alerts(ctf_event_active) == {}


def test_submissions_close_at_the_end_instant(ctf_event_active):
    from freezegun import freeze_time

    from ctf.services.challenge._access import _assert_event_active_and_in_window

    with freeze_time(ctf_event_active.event_end - timedelta(microseconds=1)):
        _assert_event_active_and_in_window(ctf_event_active)
    with freeze_time(ctf_event_active.event_end), pytest.raises(CTFStateError):
        _assert_event_active_and_in_window(ctf_event_active)


def test_spin_up_window_is_the_time_left_before_start():
    from ctf.management.commands.run_ctf_scheduler import spin_up_window_seconds

    now = timezone.now()
    assert spin_up_window_seconds(now + timedelta(minutes=10), 30, now) == 600
    assert spin_up_window_seconds(now + timedelta(hours=2), 30, now) == 1800
    assert spin_up_window_seconds(now - timedelta(minutes=5), 30, now) == 0


def test_spin_up_yields_to_due_work_and_resumes_as_a_continuation(ctf_event, organizer_credential, caplog):
    import logging

    from ctf.management.commands.run_ctf_scheduler import _handle_spin_up_ranges
    from shared.operation_authority import credential_reference_fields

    event = _move(ctf_event, event_start=timezone.now() + timedelta(minutes=10))
    _participant(event, "waiting")
    spin = CTFScheduledTask.objects.create(
        event=event,
        task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        scheduled_for=timezone.now(),
        **credential_reference_fields(organizer_credential),
    )
    # A due event start is pending: the spin-up yields before it provisions anyone.
    _task(event, ScheduledTaskType.EVENT_START.value, timezone.now() - timedelta(seconds=1))

    result = _handle_spin_up_ranges(spin, shutdown_check=lambda: False)

    assert result["interrupted"] is True
    assert result["successful"] == 0
    spin.refresh_from_db()
    assert spin.metadata["continuation"] is True

    # The continuation does not repeat the first run's capacity admission.
    CTFScheduledTask.objects.filter(task_type=ScheduledTaskType.EVENT_START.value).update(
        status=ScheduledTaskStatus.COMPLETED.value
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ctf.services.range.capacity"):
        _handle_spin_up_ranges(spin, shutdown_check=lambda: True)
    assert "capacity for event" not in caplog.text


def test_refused_start_task_is_requeued_with_backoff_not_completed(ctf_event, content_not_ready):
    from ctf.management.commands import run_ctf_scheduler as cmd

    event = _due_start(ctf_event)
    _task(event, ScheduledTaskType.EVENT_START.value)
    command = cmd.Command()
    task, token = command._claim_next_due()
    command._execute_task(task, token)

    task.refresh_from_db()
    assert task.status == ScheduledTaskStatus.PENDING.value
    assert task.retry_count == 1
    assert task.scheduled_for > timezone.now()


def test_manual_end_of_a_paused_event_stays_refused(ctf_event_active):
    from ctf.services.event import complete_event

    event = _move(ctf_event_active, status=EventStatus.PAUSED.value)
    assert complete_event(event) is False
    event.refresh_from_db()
    assert event.status == EventStatus.PAUSED.value


def test_start_alert_survives_an_exhausted_system_authoring_budget(ctf_event, settings):
    from ctf.exceptions import CTFCommunicationError
    from ctf.management.commands.run_ctf_scheduler import _handle_event_start
    from ctf.services.notification import send_reminder

    settings.CTF_COMMUNICATION_RATE_PER_ACTOR = 1
    event = _due_start(ctf_event)
    send_reminder(event.pk, hours_before=1)
    with pytest.raises(CTFCommunicationError) as exhausted:
        send_reminder(event.pk, hours_before=2)
    assert exhausted.value.code == "CTF_COMMUNICATION_RATE_LIMITED"

    _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))

    event.refresh_from_db()
    assert event.status == EventStatus.ACTIVE.value
    assert "Event started automatically" in _alerts(event)


@pytest.fixture
def unresolvable_alert_scope(ctf_event):
    """Make the event's account scope unresolvable, so alert declaration refuses."""
    from workspaces.models import Account, Organization

    account = Account.objects.get(id=ctf_event.account_id)
    organization = Organization.objects.create(account=account, name="Synthetic blocker")
    return organization


def test_failed_start_alert_is_retained_on_the_task_and_recovered(ctf_event, unresolvable_alert_scope):
    from ctf.management.commands.run_ctf_scheduler import AlertDeclarationPending, _handle_event_start

    event = _due_start(ctf_event)
    task = _task(event, ScheduledTaskType.EVENT_START.value)
    with pytest.raises(AlertDeclarationPending):
        _handle_event_start(task)

    event.refresh_from_db()
    assert event.status == EventStatus.ACTIVE.value
    assert _alerts(event) == {}
    task.refresh_from_db()
    assert task.metadata["pending_alerts"] == [{"kind": "event_started", "occurrence": event.event_start.isoformat()}]

    unresolvable_alert_scope.delete()
    task.refresh_from_db()
    assert _handle_event_start(task) is None

    assert "Event started automatically" in _alerts(event)
    task.refresh_from_db()
    assert task.metadata["pending_alerts"] == []


def test_failed_end_alert_is_retained_on_the_task_and_recovered(ctf_event_active):
    from ctf.management.commands.run_ctf_scheduler import AlertDeclarationPending, _handle_event_end
    from workspaces.models import Account, Organization

    event = _move(ctf_event_active, event_end=timezone.now() - timedelta(minutes=1))
    blocker = Organization.objects.create(account=Account.objects.get(id=event.account_id), name="Synthetic blocker")
    task = _task(event, ScheduledTaskType.EVENT_END.value, event.event_end)
    with pytest.raises(AlertDeclarationPending):
        _handle_event_end(task)
    event.refresh_from_db()
    assert event.status == EventStatus.ENDED.value

    blocker.delete()
    task.refresh_from_db()
    _handle_event_end(task)
    assert "Event ended automatically" in _alerts(event)


def test_start_retry_after_the_end_time_never_activates(ctf_event, content_not_ready, settings):
    from ctf.management.commands.run_ctf_scheduler import AutomaticStartRefused, _handle_event_start

    event = _due_start(ctf_event)
    with pytest.raises(AutomaticStartRefused):
        _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value))
    # The end time passes while content stays unready; then content recovers.
    event = _move(event, event_end=timezone.now() - timedelta(seconds=1))
    settings.CTF_CONTENT_REFERENCES = content_not_ready

    assert _handle_event_start(_task(event, ScheduledTaskType.EVENT_START.value)) is None

    event.refresh_from_db()
    assert event.status == EventStatus.REGISTRATION.value
    assert "Event started automatically" not in _alerts(event)
