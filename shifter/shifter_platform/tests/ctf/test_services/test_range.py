"""Tests for CTF Range service.

Unit tests — mock all ORM access. We test our service logic
(branching, error wrapping, return values), not SQLite.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from django.utils import timezone

from cms.models import RangeInstance
from cms.models import Request as CMSRequest
from ctf.bridges import RangeProvisionResult
from ctf.enums import ParticipantStatus, ScheduledTaskStatus, ScheduledTaskType
from ctf.exceptions import CTFNotFoundError, CTFRangeError
from ctf.models import CTFParticipant, CTFScheduledTask
from ctf.services import range as range_service
from ctf.services.range import batch, provision
from engine.models import Instance, Range
from engine.models import Request as EngineRequest
from shared.enums import RangeSource, RequestType, ResourceStatus

from .test_range_recovery import event_with_scenario  # noqa: F401 -- shared native range fixture

# Opaque #1325 workspace scope binding (ADR-046-R3). These suites do not
# exercise tenancy; a fixed scalar stands in for the value the CMS launch
# facade resolves in production.
_WORKSPACE_ID = 1


def _make_unregistered_participant(event, idx):
    """Create an unassigned, unregistered participant on ``event``.

    With no linked user, provisioning fails fast ("must be registered") before
    any CMS/network call, so the throttled loop runs without external boundaries.
    """
    return CTFParticipant.objects.create(
        event=event,
        user=None,
        email=f"throttle-{idx}@test.com",
        name=f"Throttle Participant {idx}",
        status=ParticipantStatus.REGISTERED.value,
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_participant():
    """Mock CTFParticipant with sensible defaults."""
    p = Mock(spec=CTFParticipant)
    p.pk = uuid4()
    p.range_instance_id = None
    p.range_status = ""
    p.user_id = None
    p.user = Mock(email="participant@test.com")
    p.event = Mock(scenario_id="basic", range_config=None)
    return p


@pytest.fixture
def mock_participant_with_range(mock_participant):
    """Mock participant that already has a range assigned."""
    mock_participant.range_instance_id = 42
    mock_participant.range_status = "ready"
    return mock_participant


@pytest.fixture
def _patch_participant_get(mock_participant):
    """Patch CTFParticipant.objects so .get() and .select_related().get() return mock_participant."""
    with patch.object(CTFParticipant, "objects") as mock_objects:
        mock_objects.get.return_value = mock_participant
        mock_objects.select_related.return_value.get.return_value = mock_participant
        mock_objects.DoesNotExist = CTFParticipant.DoesNotExist
        yield mock_objects


@pytest.fixture
def _patch_participant_not_found():
    """Patch CTFParticipant.objects so .get() raises DoesNotExist."""
    with patch.object(CTFParticipant, "objects") as mock_objects:
        mock_objects.get.side_effect = CTFParticipant.DoesNotExist
        mock_objects.select_related.return_value.get.side_effect = CTFParticipant.DoesNotExist
        mock_objects.DoesNotExist = CTFParticipant.DoesNotExist
        yield mock_objects


class TestProvisionParticipantRange:
    """Tests for provision_participant_range.

    DB-backed (#942): the assignment claim runs under ``transaction.atomic()`` +
    ``select_for_update()`` to close the manual/scheduled double-assign race,
    so these exercise real rows rather than a mocked ``objects`` manager.
    """

    @pytest.mark.django_db
    def test_not_found(self, organizer_credential):
        """Raises CTFNotFoundError for nonexistent participant."""
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.provision_participant_range(uuid4_2, actor=organizer_credential)

    @pytest.mark.django_db
    def test_already_assigned_raises_and_keeps_assignment(self, ctf_participant, organizer_credential):
        """An already-assigned participant raises without re-provisioning (CTF-7).

        No CMS mock: if the guard regressed and the code reached provisioning,
        the real bridge would raise "Range provisioning failed" and the
        already-has-a-range match would not be met. The original assignment must
        survive unchanged.
        """
        ctf_participant.range_instance_id = 42
        ctf_participant.save(update_fields=["range_instance_id", "updated_at"])

        with pytest.raises(CTFRangeError, match="already has a range"):
            range_service.provision_participant_range(ctf_participant.pk, actor=organizer_credential)

        ctf_participant.refresh_from_db()
        assert ctf_participant.range_instance_id == 42

    @pytest.mark.django_db
    @pytest.mark.usefixtures("event_with_scenario")
    def test_provision_success_persists_assignment(self, ctf_participant, organizer_credential, monkeypatch):
        """The real queue is empty until CTF records its exact runtime association."""
        from engine.models import ProvisionerLaunchIntent
        from engine.services._launch_authority import authorize_launch_authority
        from shared.authorization import configured_authorization_provider

        provider = configured_authorization_provider()
        original = provider.check
        observed = []

        def check(request):
            for intent in ProvisionerLaunchIntent.objects.filter(payload__operation="provision"):
                runtime = Range.objects.get(request__request_id=intent.payload["request_id"])
                assert CTFParticipant.objects.filter(pk=ctf_participant.pk, range_instance_id__isnull=False).exists()
                observed.append(runtime.pk)
            return original(request)

        monkeypatch.setattr(provider, "check", check)
        result = range_service.provision_participant_range(ctf_participant.pk, actor=organizer_credential)
        ctf_participant.refresh_from_db()
        assert result["range_instance_id"] == ctf_participant.range_instance_id
        intent = ProvisionerLaunchIntent.objects.get(payload__operation="provision")
        runtime = Range.objects.get(request__request_id=intent.payload["request_id"])
        authorize_launch_authority(intent, runtime)
        assert observed == [runtime.pk]

    @pytest.mark.django_db
    def test_in_flight_provisioning_blocks_reprovision(self, ctf_participant, organizer_credential):
        """A participant mid-provision (status set, no instance id) is treated as claimed (#942).

        cms_create_range can succeed while cms_find_range_instance_id returns
        None, leaving range_status="provisioning" with a null range_instance_id.
        Keying the guard only on range_instance_id would let the next caller
        create a second CMS range. No CMS mock here: if the guard regressed the
        real bridge would raise "Range provisioning failed", not the benign
        "already has a range".
        """
        ctf_participant.range_status = "provisioning"
        ctf_participant.save(update_fields=["range_status", "updated_at"])

        with pytest.raises(CTFRangeError, match="already has a range"):
            range_service.provision_participant_range(ctf_participant.pk, actor=organizer_credential)

        ctf_participant.refresh_from_db()
        assert ctf_participant.range_instance_id is None
        assert ctf_participant.range_status == "provisioning"

    @pytest.mark.django_db
    def test_provision_requires_registered_user(self, ctf_participant_no_account, organizer_credential):
        """Raises CTFRangeError if participant has no linked user."""
        with pytest.raises(CTFRangeError, match="must be registered"):
            range_service.provision_participant_range(ctf_participant_no_account.pk, actor=organizer_credential)

    @pytest.mark.django_db
    def test_provision_cms_failure(self, ctf_participant, organizer_credential):
        """A real unavailable source leaves one durable request for a retry."""
        from ctf.models import CTFRangeLaunch

        with pytest.raises(CTFRangeError, match="Range provisioning failed"):
            range_service.provision_participant_range(ctf_participant.pk, actor=organizer_credential)

        ctf_participant.refresh_from_db()
        assert ctf_participant.range_status == "provisioning"
        assert ctf_participant.range_request_id == CTFRangeLaunch.objects.get().pk
        assert ctf_participant.range_instance_id is None

    @pytest.mark.django_db
    def test_policy_denial_propagates_permanent_code(
        self, ctf_participant, organizer_credential, settings, monkeypatch
    ):
        """A real GDC live-fire denial reaches provision with its permanent code intact (#1348).

        No mock: under gcp+gdc the real CMS create_range gate raises a CMSError carrying
        the ADR-039 identity-or-policy code, so this exercises the actual
        ``code=_underlying_policy_code(e)`` propagation line the retry wrapper depends on
        (a regression there would silently downgrade the denial to a retryable error).
        """
        from shared.range_instantiation_policy import POLICY_DENIAL_CODE

        settings.CLOUD_PROVIDER = "gcp"
        monkeypatch.setenv("GCP_RANGE_BACKEND", "gdc")

        with pytest.raises(CTFRangeError) as exc:
            range_service.provision_participant_range(ctf_participant.pk, actor=organizer_credential)

        assert exc.value.code == POLICY_DENIAL_CODE


class TestGetRangeStatus:
    """Tests for get_range_status."""

    def test_not_found(self, _patch_participant_not_found):
        """Raises CTFNotFoundError for nonexistent participant."""
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.get_range_status(uuid4_2)

    @pytest.mark.usefixtures("_patch_participant_get")
    def test_not_assigned(self, mock_participant):
        """Returns not_assigned when no range."""
        result = range_service.get_range_status(mock_participant.pk)
        assert result["status"] == "not_assigned"

    @pytest.mark.usefixtures("_patch_participant_get")
    @pytest.mark.parametrize(
        ("cached_status", "expect_save"),
        [
            ("provisioning", True),  # status changed -> cache refreshed (save)
            ("ready", False),  # status unchanged -> no redundant DB write
        ],
    )
    def test_polls_cms(self, mock_participant, cached_status, expect_save):
        """Queries CMS for fresh status; saves only when the cached value changed."""
        mock_participant.range_instance_id = 42
        mock_participant.range_status = cached_status

        with patch("ctf.bridges.cms_get_range_status", return_value="ready"):
            result = range_service.get_range_status(mock_participant.pk)

        assert result["status"] == "ready"
        assert result["vpn_profile_available"] is False
        assert mock_participant.range_status == "ready"
        if expect_save:
            mock_participant.save.assert_called_once()
        else:
            mock_participant.save.assert_not_called()

    @pytest.mark.django_db
    def test_omits_vpn_profile_availability_without_a_live_credential(self, ctf_participant):
        """Do not project credential availability without live participant proof."""
        from workspaces.services import resolve_personal_workspace

        user = ctf_participant.user
        workspace_id = resolve_personal_workspace(user).workspace_id
        request_id = uuid4()
        cms_request = CMSRequest.objects.create(
            workspace_id=workspace_id,
            request_id=request_id,
            request_type=RequestType.RANGE.value,
            user=user,
        )
        cms_range = RangeInstance.objects.create(
            workspace_id=workspace_id,
            request=cms_request,
            scenario_id="basic",
            user_id=user.id,
            status=ResourceStatus.READY.value,
            range_source=RangeSource.CTF.value,
        )
        engine_request = EngineRequest.objects.create(
            request_id=request_id,
            request_type=RequestType.RANGE.value,
            user=user,
        )
        target_ref = uuid4()
        Instance.objects.create(
            uuid=target_ref,
            request=engine_request,
            role=Instance.Role.ATTACKER,
            os_type=Instance.OSType.KALI,
            status=Range.Status.READY,
        )
        Range.objects.create(
            workspace_id=workspace_id,
            request=engine_request,
            user=user,
            status=Range.Status.READY,
            vpn_access_binding={
                "version": "openvpn-binding-v1",
                "channel": "openvpn",
                "generation": str(request_id),
                "owner_user_id": user.id,
                "target_ref": str(target_ref),
                "endpoint": "vpn.example.test",
                "port": 1194,
                "profile_version": "openvpn-profile-v1",
                "secret_ref": "arn:aws:secretsmanager:eu-central-1:123:secret:range-vpn",
                "ready": True,
            },
        )
        ctf_participant.range_instance_id = cms_range.pk
        ctf_participant.range_status = "provisioning"
        ctf_participant.save(update_fields=["range_instance_id", "range_status", "updated_at"])

        result = range_service.get_range_status(ctf_participant.pk)

        assert result["status"] == "ready"
        assert result["vpn_profile_available"] is False
        ctf_participant.refresh_from_db()
        assert ctf_participant.range_status == "ready"


class TestCleanupEventRanges:
    """Tests for cleanup_event_ranges."""

    def test_not_found(self):
        """Raises CTFNotFoundError for nonexistent event."""
        from ctf.models import CTFEvent

        with patch.object(CTFEvent, "objects") as mock_objects:
            mock_objects.get.side_effect = CTFEvent.DoesNotExist
            mock_objects.DoesNotExist = CTFEvent.DoesNotExist
            uuid4_2 = uuid4()
            with pytest.raises(CTFNotFoundError):
                range_service.cleanup_event_ranges(uuid4_2, actor=Mock())


class TestDestroyParticipantRange:
    """Tests for destroy_participant_range."""

    def test_not_found(self, _patch_participant_not_found):
        """Raises CTFNotFoundError for nonexistent participant."""
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.destroy_participant_range(uuid4_2, actor=Mock())


# ---------------------------------------------------------------------------
# Throttled provisioning fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_sleep():
    """Patch time.sleep (the OS boundary) so throttled tests do not really wait."""
    with patch("time.sleep") as mock_sleep:
        yield mock_sleep


def _make_skip_participant(event, user, idx):
    """Create a registered participant mid-provision (range_status='provisioning').

    With a null ``range_instance_id`` it is still selected by the throttle query,
    but the assignment guard rejects it as 'already has a range' before any CMS
    call, so the loop counts it as a benign skip (#942) with no network I/O.
    """
    return CTFParticipant.objects.create(
        event=event,
        user=user,
        email=f"skip-{idx}@test.com",
        name=f"Skip Participant {idx}",
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
        range_status="provisioning",
    )


class TestProvisionEventRangesThrottled:
    """Tests for provision_event_ranges_throttled.

    DB-backed: outcomes are driven by participant data rather than by mocking the
    internal provision call. Unregistered participants fail fast ('must be
    registered') before any CMS/network call; a mid-provision participant is a
    benign skip. ``time.sleep`` is patched at the OS boundary so pacing is instant.
    """

    @pytest.mark.django_db
    def test_not_found(self):
        """Raises CTFNotFoundError for nonexistent event."""
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.provision_event_ranges_throttled(uuid4_2, 300)

    @pytest.mark.django_db
    def test_empty_participants(self, ctf_event, organizer_credential):
        """Returns zeros when no participants need provisioning."""
        result = range_service.provision_event_ranges_throttled(ctf_event.pk, 300, actor=organizer_credential)

        assert result["total"] == 0
        assert result["successful"] == 0
        assert result["failed"] == 0
        assert result["interrupted"] is False

    @pytest.mark.django_db
    @pytest.mark.usefixtures("_patch_sleep")
    def test_failures_and_skips_tallied_with_notification(self, ctf_event, participant_user, organizer_credential):
        """Failures and benign skips are tallied separately; failures notify the organizer."""
        _make_unregistered_participant(ctf_event, 0)
        _make_unregistered_participant(ctf_event, 1)
        _make_skip_participant(ctf_event, participant_user, 0)

        with patch("ctf.services.notification.notify_organizer_provision_failure") as mock_notify:
            result = range_service.provision_event_ranges_throttled(ctf_event.pk, 300, actor=organizer_credential)

        assert result["total"] == 3
        assert result["successful"] == 0
        assert result["failed"] == 2
        assert result["skipped"] == 1
        assert len(result["errors"]) == 2
        mock_notify.assert_called_once()

    @pytest.mark.django_db
    def test_pacing_sleeps_between_provisions_only(self, ctf_event, organizer_credential, _patch_sleep):
        """Sleeps fill the inter-provision gaps (one fewer than participants), each summing to the clamped delay."""
        for i in range(3):
            _make_unregistered_participant(ctf_event, i)

        range_service.provision_event_ranges_throttled(ctf_event.pk, 300, actor=organizer_credential)

        # window=300 / 3 participants = 100s clamped; two gaps; chunked sleeps sum to 200s.
        assert sum(c.args[0] for c in _patch_sleep.call_args_list) == 200.0

    @pytest.mark.django_db
    @pytest.mark.usefixtures("_patch_sleep")
    def test_shutdown_interruption(self, ctf_event, organizer_credential):
        """shutdown_check stops the loop and sets interrupted=True."""
        for i in range(5):
            _make_unregistered_participant(ctf_event, i)

        calls = {"n": 0}

        def shutdown_check():
            # False for the first iteration's top-of-loop check, True thereafter,
            # so exactly one participant is processed before the loop breaks.
            calls["n"] += 1
            return calls["n"] > 1

        result = range_service.provision_event_ranges_throttled(
            ctf_event.pk, 600, shutdown_check=shutdown_check, actor=organizer_credential
        )

        assert result["interrupted"] is True
        assert result["total"] == 1

    @pytest.mark.django_db
    @pytest.mark.usefixtures("_patch_sleep")
    def test_heartbeat_called_each_iteration(self, ctf_event, organizer_credential):
        """The heartbeat fires at least once per participant so the task stays live.

        DB-backed with unregistered participants: provisioning fails fast before
        any CMS/network call. The heartbeat also fires during the chunked
        inter-provision waits (#943), so the count is >= the participant count.
        """
        for i in range(3):
            _make_unregistered_participant(ctf_event, i)
        heartbeat = Mock()

        result = range_service.provision_event_ranges_throttled(
            ctf_event.pk, 300, heartbeat=heartbeat, actor=organizer_credential
        )

        assert heartbeat.call_count >= 3
        assert result["total"] == 3

    @pytest.mark.django_db
    @pytest.mark.usefixtures("_patch_sleep")
    def test_heartbeat_failure_does_not_abort_spin_up(self, ctf_event, organizer_credential):
        """A failing heartbeat is swallowed; the spin-up loop still runs to completion."""
        for i in range(3):
            _make_unregistered_participant(ctf_event, i)
        heartbeat = Mock(side_effect=RuntimeError("db hiccup"))

        result = range_service.provision_event_ranges_throttled(
            ctf_event.pk, 300, heartbeat=heartbeat, actor=organizer_credential
        )

        assert heartbeat.call_count >= 3
        assert result["total"] == 3


class TestComputeThrottleDelay:
    """compute_throttle_delay is the pure pacing seam: window/count clamped to [5, 120]s."""

    def test_passthrough_within_band(self):
        # 100s window / 2 participants = 50s raw, inside the band.
        assert batch.compute_throttle_delay(100, 2) == 50.0

    def test_floor_clamp(self):
        # 2s / 2 = 1s raw -> clamped up to the 5s floor.
        assert batch.compute_throttle_delay(2, 2) == 5.0

    def test_ceiling_clamp(self):
        # 500s / 2 = 250s raw -> clamped down to the 120s ceiling.
        assert batch.compute_throttle_delay(500, 2) == 120.0

    def test_zero_participants_does_not_divide_by_zero(self):
        # Guarded by max(count, 1); the window divides by 1 then clamps.
        assert batch.compute_throttle_delay(60, 0) == 60.0


class TestIsAlreadyAssignedError:
    """The benign race-loser discriminator that keeps the skip path off the failure path (CTF-7)."""

    def test_already_assigned_message_is_benign(self):
        assert provision._is_already_assigned_error(CTFRangeError("Participant already has a range assigned"))

    def test_other_range_error_is_not_benign(self):
        assert not provision._is_already_assigned_error(CTFRangeError("Range provisioning failed: boom"))

    def test_non_range_error_is_not_benign(self):
        assert not provision._is_already_assigned_error(RuntimeError("boom"))


class TestPermanentProvisioningError:
    """The retry wrapper must not retry permanent failures (issue #1348).

    A GDC live-fire policy denial is permanent: CMS carries the ADR-039
    ``identity-or-policy`` code on ``CMSError.details``, provision propagates it onto
    the ``CTFRangeError``, and the retry loop treats that code (and the existing
    validation messages) as non-retryable rather than re-running the same denial for
    every backoff attempt.
    """

    def test_policy_denial_code_is_permanent(self):
        from shared.range_instantiation_policy import POLICY_DENIAL_CODE

        err = CTFRangeError("denied", code=POLICY_DENIAL_CODE)
        assert provision._is_permanent_provisioning_error(err) is True

    def test_validation_messages_are_permanent(self):
        assert provision._is_permanent_provisioning_error(CTFRangeError("user must be registered"))
        assert provision._is_permanent_provisioning_error(CTFRangeError("already has a range"))

    def test_generic_provisioning_failure_is_retryable(self):
        assert provision._is_permanent_provisioning_error(CTFRangeError("Range provisioning failed: boom")) is False

    def test_prerequisite_code_is_retryable(self):
        # A prerequisite (config) failure is not the permanent policy denial, so it
        # stays on the normal retry path.
        from shared.range_instantiation_policy import PREREQUISITE_DENIAL_CODE

        err = CTFRangeError("misconfigured", code=PREREQUISITE_DENIAL_CODE)
        assert provision._is_permanent_provisioning_error(err) is False

    def test_underlying_policy_code_extracts_from_details(self):
        from cms.exceptions import CMSError
        from shared.range_instantiation_policy import POLICY_DENIAL_CODE

        cause = CMSError("denied", details={"code": POLICY_DENIAL_CODE})
        assert provision._underlying_policy_code(cause) == POLICY_DENIAL_CODE

    def test_underlying_policy_code_none_when_absent(self):
        assert provision._underlying_policy_code(RuntimeError("boom")) is None


class TestInterruptibleSleep:
    """_interruptible_sleep is the keep-alive primitive used by the throttled
    loop and the retry backoff: it sleeps the full duration in chunks while
    touching the heartbeat and honoring shutdown."""

    def test_chunks_sum_to_duration_and_touch_heartbeat(self, _patch_sleep):
        heartbeat = Mock()

        provision._interruptible_sleep(40, heartbeat=heartbeat)

        # Slept the whole 40s in <=15s chunks (15 + 15 + 10).
        assert sum(c.args[0] for c in _patch_sleep.call_args_list) == 40
        assert heartbeat.call_count >= 3

    def test_aborts_early_on_shutdown(self, _patch_sleep):
        provision._interruptible_sleep(120, shutdown_check=lambda: True)

        # Shutdown is checked before the first chunk, so nothing is slept.
        assert _patch_sleep.call_count == 0


@pytest.mark.django_db
class TestRequestEventProvisioning:
    """request_event_provisioning enqueues/coalesces a SPIN_UP_RANGES task."""

    def _spin_up_count(self, event):
        return CTFScheduledTask.objects.filter(
            event=event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        ).count()

    def test_creates_due_now_task_when_none_exists(self, ctf_event, organizer_credential):
        before = timezone.now()

        task = range_service.request_event_provisioning(ctf_event.id, source="manual", actor_id=organizer_credential)

        assert task.task_type == ScheduledTaskType.SPIN_UP_RANGES.value
        assert task.status == ScheduledTaskStatus.PENDING.value
        assert before - timedelta(seconds=2) <= task.scheduled_for <= timezone.now()
        assert task.metadata.get("source") == "manual"
        assert self._spin_up_count(ctf_event) == 1

    def test_coalesces_future_pending_pulled_to_now(self, ctf_event, organizer_credential):
        from shared.operation_authority import credential_reference_fields

        existing = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now() + timedelta(hours=6),
            **credential_reference_fields(organizer_credential),
        )

        task = range_service.request_event_provisioning(ctf_event.id, source="manual", actor_id=organizer_credential)

        assert task.pk == existing.pk
        task.refresh_from_db()
        assert task.scheduled_for <= timezone.now()
        assert task.metadata.get("source") == "manual"
        # No duplicate runnable task was created.
        assert self._spin_up_count(ctf_event) == 1

    def test_reuses_running_task(self, ctf_event, organizer_credential):
        from shared.operation_authority import credential_reference_fields

        existing = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now() - timedelta(minutes=1),
            status=ScheduledTaskStatus.RUNNING.value,
            **credential_reference_fields(organizer_credential),
        )

        task = range_service.request_event_provisioning(ctf_event.id, actor_id=organizer_credential)

        assert task.pk == existing.pk
        assert task.status == ScheduledTaskStatus.RUNNING.value
        assert self._spin_up_count(ctf_event) == 1

    def test_event_not_found_raises(self, organizer_credential):
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.request_event_provisioning(uuid4_2, actor_id=organizer_credential)


@pytest.mark.django_db
class TestGetProvisionProgress:
    """get_provision_progress projects participant counts plus the active task."""

    def _participant(self, event, name, range_status, range_instance_id=None):
        return CTFParticipant.objects.create(
            event=event,
            email=f"{name}@test.com",
            name=name,
            range_status=range_status,
            range_instance_id=range_instance_id,
        )

    def test_counts_and_active_task(self, ctf_event):
        self._participant(ctf_event, "a", "ready", 1)
        self._participant(ctf_event, "b", "provisioning", 2)
        self._participant(ctf_event, "c", "error")
        self._participant(ctf_event, "d", "")  # not assigned
        task = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now(),
        )

        progress = range_service.get_provision_progress(ctf_event.id)

        counts = progress["counts"]
        assert counts["total"] == 4
        assert counts["ready"] == 1
        assert counts["provisioning"] == 1
        assert counts["error"] == 1
        assert counts["not_assigned"] == 1
        assert progress["task"]["id"] == str(task.pk)
        assert progress["task"]["status"] == ScheduledTaskStatus.PENDING.value

    def test_no_active_task(self, ctf_event):
        progress = range_service.get_provision_progress(ctf_event.id)

        assert progress["task"] is None
        assert progress["counts"]["total"] == 0


class TestCmsBridgeRangeSource:
    """Bridge seam: cms_create_range must forward range_source=RangeSource.CTF (#450).

    DB-backed behavior test. It drives the real bridge and RAES persistence stack,
    holding only cloud dispatch at the seam, and asserts CTF provenance and the
    server-derived event deadline are retained.
    """

    @pytest.fixture
    def _ctf_raes_source(self, ctf_event, organizer_user, monkeypatch, settings):
        from cms.models import RaesPackageSource
        from cms.raes.dispatch import CmsRaesDispatchPort
        from workspaces.services import resource_scope_from_ids

        monkeypatch.setattr("engine.services._raes_range.start_raes_range_provisioning", lambda *_a, **_kw: None)
        settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
        settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64

        def dispatch(request_id, credential, source, options):
            port = CmsRaesDispatchPort(
                user_id=None,
                request_id=str(request_id),
                workspace_id=options.workspace_id,
                backend_admission=options.backend_admission,
                egress_mode=options.egress_mode,
                credential=credential,
                scope=options.principal_scope,
                source=source,
                owner_principal=options.owner_principal,
                authority_action=options.authority_action,
                authority_target=options.authority_target,
            )
            port.realize({"kind": "raes_provisioning_plan", "raes_version": "2.0", "resources": {}})

        monkeypatch.setattr("cms.services._raes_dispatch.dispatch_repo_raes_package", dispatch)
        scope = resource_scope_from_ids(
            kind=ctf_event.scope_kind,
            account_id=ctf_event.account_id,
            organization_id=ctf_event.organization_id,
            workspace_id=ctf_event.workspace_id,
        )
        return RaesPackageSource.objects.create(
            scenario_id="ctf-bridge-test",
            contract_kind="raes",
            contract_profile="shifter",
            package_ref="tests/packs/ctf-bridge-test",
            package_version="1.0.0",
            package_digest="sha256:" + "a" * 64,
            conformance_status="passed",
            registered_by=organizer_user,
            owner_principal_uuid=ctf_event.owner_principal_uuid,
            scope_kind="account",
            account_uuid=scope.account_uuid,
        )

    def test_cms_create_range_persists_ctf_range_source(
        self, participant_user, ctf_participant, ctf_event, organizer_credential, _ctf_raes_source
    ):
        """ctf.bridges.cms_create_range stores the range with CTF provenance."""
        from cms.models import RangeInstance
        from ctf.bridges import CTFRangeLaunchOptions, cms_create_range
        from engine.models import Range as EngineRange
        from management.services import resolve_principal_uuid
        from shared.enums import RangeSource
        from workspaces.services import resource_scope_from_ids

        teardown_at = timezone.now() + timedelta(days=5)
        scope = resource_scope_from_ids(
            kind=ctf_event.scope_kind,
            account_id=ctf_event.account_id,
            organization_id=ctf_event.organization_id,
            workspace_id=ctf_event.workspace_id,
        )
        result = cms_create_range(
            scenario=_ctf_raes_source.scenario_id,
            agents_by_os={},
            ngfw_enabled=False,
            remote_access_teardown_at=teardown_at,
            launch_options=CTFRangeLaunchOptions(
                credential=organizer_credential,
                scope=scope,
                owner_principal=resolve_principal_uuid(ctf_participant.principal_uuid),
                event_uuid=ctf_event.pk,
            ),
        )

        assert isinstance(result, RangeProvisionResult)
        instance = RangeInstance.objects.get(request__request_id=result.request_id)
        assert instance.range_source == RangeSource.CTF.value
        assert instance.user_id == participant_user.id
        assert instance.expires_at == teardown_at
        assert instance.maximum_expires_at == teardown_at
        assert EngineRange.objects.get(request__request_id=result.request_id).remote_access_capability is None
