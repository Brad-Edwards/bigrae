"""Scheduled communication declarations and due-time release (#2099, CTF-010).

A scheduled declaration commits the intent and its release task together and does
NOT materialize an audience; release happens only at the eligible occurrence,
under the same admission re-check and a closed lateness/expiry policy. Repeated
ticks and restarts are idempotent. Each test drives the real services.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from ctf.enums import ParticipantStatus, ScheduledTaskStatus, ScheduledTaskType
from ctf.enums_communication import IntentStatus
from ctf.exceptions import CTFCommunicationError
from ctf.models import (
    CommunicationIntent,
    CTFParticipant,
    CTFScheduledTask,
    RecipientSnapshot,
)
from ctf.services.communication import (
    AdmissionActor,
    CampaignDraft,
    create_campaign,
    release_due_declaration,
    request_early_release,
    run_release_communication_task,
    schedule_declaration,
)
from shared.api_tokens.models import ApiToken
from shared.api_tokens.policy import personal_credential_for_token
from shared.api_tokens.scopes import CTF_COMMUNICATION_WRITE
from shared.principal_port import principal_for_user
from workspaces.models import Account

pytestmark = pytest.mark.django_db
User = get_user_model()


def _account_uuid(user):
    principal = principal_for_user(user)
    account, _ = Account.objects.get_or_create(
        individual_principal_uuid=principal.uuid,
        defaults={"kind": Account.Kind.INDIVIDUAL, "name": f"Test account {user.pk}"},
    )
    return str(account.uuid)


def _credential(user):
    token, _ = ApiToken.create_token(name="scheduled communication", created_by=user, scopes=[CTF_COMMUNICATION_WRITE])
    return personal_credential_for_token(token)


def _scheduled(organizer_user, ctf_event, *, due_at, occurrence="occ-sched"):
    CTFParticipant.objects.create(
        event=ctf_event,
        email="a@test.com",
        name="a",
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
    )
    credential = _credential(organizer_user)
    campaign = create_campaign(
        credential,
        _account_uuid(organizer_user),
        CampaignDraft(
            title="Kickoff",
            origin="organizer_staff",
            target_event_ids=[ctf_event.id],
            audience_spec={"kind": "event", "event_ids": [str(ctf_event.id)]},
            trigger_spec={"kind": "absolute_time", "due_at": due_at.isoformat()},
            channels=["in_app"],
            subject="Welcome",
            body="Hello",
        ),
        parent_type="account",
    )
    intent = schedule_declaration(
        campaign,
        due_at=due_at,
        occurrence_key=occurrence,
        actor=AdmissionActor(credential=credential),
    )
    return campaign, intent, credential


def test_schedule_commits_a_scheduled_intent_and_task_without_materializing(organizer_user, ctf_event):
    due = timezone.now() + timezone.timedelta(hours=6)
    _campaign, intent, _credential_context = _scheduled(organizer_user, ctf_event, due_at=due)

    assert intent.status == IntentStatus.SCHEDULED.value
    assert intent.due_at == due
    # No audience materialized at scheduling: future content is never inbox-visible.
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)
    assert task.scheduled_for == due
    assert task.metadata["intent_id"] == str(intent.id)
    assert task.status == ScheduledTaskStatus.PENDING.value


def test_due_release_materializes_and_is_idempotent(organizer_user, ctf_event):
    due = timezone.now() - timezone.timedelta(minutes=1)  # due, within grace
    _campaign, intent, credential = _scheduled(organizer_user, ctf_event, due_at=due)

    first = release_due_declaration(intent, actor=AdmissionActor(credential=credential))
    assert first == "released"
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.RELEASED.value
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1

    # A repeated tick/restart observes the release; it does not re-materialize.
    second = release_due_declaration(intent, actor=AdmissionActor(credential=credential))
    assert second == "released"
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1


def test_release_expires_past_the_grace_window(organizer_user, ctf_event, settings):
    settings.CTF_COMMUNICATION_RELEASE_GRACE_MINUTES = 20
    due = timezone.now() - timezone.timedelta(minutes=45)  # well past grace
    _campaign, intent, credential = _scheduled(organizer_user, ctf_event, due_at=due)

    outcome = release_due_declaration(intent, actor=AdmissionActor(credential=credential))

    assert outcome == "expired"
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.EXPIRED.value
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()  # durable no-work


def test_due_release_revalidates_and_denies_a_revoked_actor(organizer_user, ctf_event):
    due = timezone.now() - timezone.timedelta(minutes=1)
    _campaign, intent, credential = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)

    ApiToken.objects.get(credential_uuid=credential.credential_uuid).revoke()

    result = run_release_communication_task(task)
    assert result["outcome"] == "denied"
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.EXPIRED.value
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


def test_scheduler_handler_reschedules_when_not_yet_due(organizer_user, ctf_event):
    due = timezone.now() + timezone.timedelta(hours=6)  # future
    _campaign, intent, _credential_context = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)

    result = run_release_communication_task(task)

    assert result["reschedule_for"] == due
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


def test_run_task_now_early_releases_a_scheduled_communication(organizer_user, ctf_event, organizer_credential):
    from ctf.enums import ScheduledTaskStatus
    from ctf.services.event.scheduling import run_task_now

    due = timezone.now() + timezone.timedelta(hours=6)
    _campaign, intent, _declaration_credential = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)

    run_task_now(ctf_event.id, task.id, actor_id=organizer_credential)

    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.RELEASED.value
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1
    task.refresh_from_db()
    assert task.status == ScheduledTaskStatus.COMPLETED.value


def test_run_task_now_denies_comm_early_release_without_notification_authority(
    organizer_user, ctf_event, live_session_credential
):
    from ctf.services.event.scheduling import run_task_now

    due = timezone.now() + timezone.timedelta(hours=6)
    _campaign, intent, _credential_context = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)
    outsider = User.objects.create_user(username="outsider", password="pw")  # nosec B106

    outsider_credential = live_session_credential(outsider)
    with pytest.raises(CTFCommunicationError):
        run_task_now(ctf_event.id, task.id, actor_id=outsider_credential)
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.SCHEDULED.value


def test_early_release_before_due_requires_the_grant(organizer_user, ctf_event, organizer_credential):
    due = timezone.now() + timezone.timedelta(hours=6)
    _campaign, intent, credential = _scheduled(organizer_user, ctf_event, due_at=due)

    # Without the explicit early-release grant, an early release is denied.
    ungranted_actor = AdmissionActor(credential=credential)
    with pytest.raises(CTFCommunicationError):
        request_early_release(intent, actor=ungranted_actor)
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.SCHEDULED.value

    # With the grant, an authorized organizer run-now releases before the due time.
    outcome = request_early_release(
        intent, actor=AdmissionActor(credential=organizer_credential, allow_early_release=True)
    )
    assert outcome == "released"
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.RELEASED.value
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1


def test_transient_backpressure_reschedules_and_keeps_the_occurrence(organizer_user, ctf_event, monkeypatch):
    from ctf.services.communication import release as release_module

    due = timezone.now() - timezone.timedelta(minutes=1)  # due, within grace
    _campaign, intent, _credential_context = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)

    def _pressure(_request):
        raise CTFCommunicationError("busy", code="CTF_COMMUNICATION_RATE_LIMITED")

    monkeypatch.setattr(release_module, "enforce_admission", _pressure)
    result = run_release_communication_task(task)

    # Transient pressure reschedules a retry; the occurrence is NOT discarded.
    assert "reschedule_for" in result
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.SCHEDULED.value
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


def test_permanent_denial_at_due_time_expires_the_declaration(organizer_user, ctf_event):
    due = timezone.now() - timezone.timedelta(minutes=1)
    _campaign, intent, _credential_context = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)
    User.objects.filter(pk=organizer_user.id).update(is_active=False)  # authority permanently revoked

    result = run_release_communication_task(task)

    # A permanent denial reaches a terminal no-work outcome, not a stranded SCHEDULED.
    assert result["outcome"] == "denied"
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.EXPIRED.value
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()


def test_run_task_now_accepts_exact_live_credential_for_early_release(organizer_user, ctf_event, organizer_credential):
    from ctf.services.event.scheduling import run_task_now

    due = timezone.now() + timezone.timedelta(hours=6)
    _campaign, intent, _declaration_credential = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(task_type=ScheduledTaskType.RELEASE_COMMUNICATION.value)

    run_task_now(ctf_event.id, task.id, actor_id=organizer_credential)
    assert CommunicationIntent.objects.get(pk=intent.pk).status == IntentStatus.RELEASED.value
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1


def test_dependency_outage_preserves_scheduled_work_then_releases_once(organizer_user, ctf_event, monkeypatch):
    from ctf.models import DeliveryAttempt
    from shared.authorization import AuthorizationDecision, DecisionKind, configured_authorization_provider

    due = timezone.now() - timezone.timedelta(minutes=1)
    _, intent, _ = _scheduled(organizer_user, ctf_event, due_at=due)
    task = CTFScheduledTask.objects.get(metadata__intent_id=str(intent.pk))
    provider = configured_authorization_provider()
    original = provider.check
    unavailable = True

    def check(request):
        if unavailable:
            return AuthorizationDecision(DecisionKind.EVALUATOR_ERROR, "synthetic_outage")
        return original(request)

    monkeypatch.setattr(provider, "check", check)
    result = run_release_communication_task(task)
    assert "reschedule_for" in result
    intent.refresh_from_db()
    assert intent.status == IntentStatus.SCHEDULED.value
    assert not RecipientSnapshot.objects.filter(intent=intent).exists()
    unavailable = False
    assert run_release_communication_task(task)["outcome"] == "released"
    assert run_release_communication_task(task)["outcome"] == "released"
    assert RecipientSnapshot.objects.filter(intent=intent).count() == 1
    assert DeliveryAttempt.objects.filter(intent=intent).count() == 1
