"""Tests for CTF destroyed-participant-range recovery (issue #1018).

DB-backed integration-style tests (per the repo's OOM lesson: fixtures and
real rows over many micro-tests with inline mocks). Per ADR-019's
boundary-mock policy, first-party ``ctf.bridges`` / ``cms.services`` targets
are not patched here at all: the ``rebuild`` strategy drives the *real*
``ctf.bridges.cms_create_range`` -> ``cms.services.create_range`` ->
the RAES engine creation stack (cloud dispatch is held at its seam -- see
``tests/cms/test_services_range.py``), and old-range teardown drives the real
``ctf.bridges.cms_destroy_range`` -> ``cms.services.destroy_range`` ->
``engine.services.destroy_range_by_request`` stack. The only mock in this
file is at the ECS/cloud boundary (``engine.ecs.start_range_teardown``), used
once to simulate a transient provider failure for the idempotent-retry test.

Everything else (RangeInstance/Range/Request rows, ownership reassignment,
participant repointing, audit) runs for real so cross-app resolution
(``engine.models.Range.resolve_active_for_instance``) is genuinely exercised.

The old range is always destroyed -- there is no disposition/forensics
concept (owner decision, #1018 revised plan). ``reassign_spare`` now consumes
an event-scoped ``CTFSpareRange`` pool row instead of scanning CMS for any
unowned same-scenario range.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from cms.models import RaesPackageSource, RangeInstance
from cms.models import Request as CmsRequest
from ctf.enums import (
    EventStatus,
    ParticipantStatus,
    RecoveryFailureCategory,
    RecoveryPhase,
    RecoveryStrategy,
    SpareRangeStatus,
)
from ctf.exceptions import CTFRangeError
from ctf.models import (
    CTFBracket,
    CTFEvent,
    CTFParticipant,
    CTFRangeRecovery,
    CTFSpareRange,
    CTFTeam,
)
from ctf.services.range.recovery import recover_participant_range
from engine.models import Range as EngineRange
from engine.models import Request as EngineRequest
from shared.audit import AuditAction
from shared.cloud.exceptions import CloudTaskError
from shared.enums import RangeSource, RequestType, ResourceStatus
from shared.models import AuditLog


def _make_spare_range(*, event: CTFEvent, owner, scenario_id: str = "basic") -> RangeInstance:
    """Create a real, minimal CTF-sourced ``cms.RangeInstance`` + engine ``Range``/``Request``.

    The raw range object underlying a pooled spare (see
    :func:`_make_pooled_spare`) or a rebuild's old range -- the ``rebuild``
    strategy drives the real ``ctf.bridges.cms_create_range`` end to end
    instead (see module docstring), so no equivalent helper is needed for new
    ranges.
    """
    from management.services import resolve_principal_uuid

    owner_principal = resolve_principal_uuid(owner.identity_principal.uuid)
    request_id = CmsRequest.objects.create(
        workspace_id=event.workspace_id,
        request_id=uuid4(),
        request_type=RequestType.RANGE.value,
        user=owner,
        owner_principal_uuid=owner_principal.uuid,
        scope_kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
    ).request_id
    engine_request = EngineRequest.objects.create(
        request_id=request_id,
        request_type=RequestType.RANGE.value,
        user=owner,
        owner_principal_uuid=owner_principal.uuid,
        scope_kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    instance_uuid = str(uuid4())
    engine_range = EngineRange.objects.create(
        workspace_id=event.workspace_id,
        uuid=uuid4(),
        user=owner,
        owner_principal_uuid=owner_principal.uuid,
        event_uuid=event.pk,
        request=engine_request,
        cms_user_id=owner.id,
        scope_kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        egress_mode="none",
        resource_generation=uuid4(),
        status=EngineRange.Status.READY,
        subnet_index=EngineRange.allocate_subnet_index(),
        provisioned_instances=[
            {"uuid": instance_uuid, "role": "attacker", "os_type": "kali", "private_ip": "10.1.1.10"}
        ],
    )
    cms_request = CmsRequest.objects.get(request_id=request_id)
    range_instance = RangeInstance.objects.create(
        workspace_id=event.workspace_id,
        request=cms_request,
        scenario_id=scenario_id,
        user_id=owner.id,
        owner_principal_uuid=owner_principal.uuid,
        event_uuid=event.pk,
        scope_kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        range_source=RangeSource.CTF.value,
        status=ResourceStatus.READY.value,
    )
    engine_range.cms_range_uuid = range_instance.uuid
    engine_range.save(update_fields=["cms_range_uuid", "updated_at"])
    range_instance.range_id = engine_range.pk
    range_instance.save(update_fields=["range_id", "updated_at"])
    range_instance.engine_range = engine_range
    range_instance.instance_uuid = instance_uuid
    return range_instance


def _make_pooled_spare(
    event: CTFEvent,
    *,
    owner,
    scenario_id: str | None = None,
    status: str = SpareRangeStatus.READY.value,
) -> tuple[CTFSpareRange, RangeInstance]:
    """Create a real CTF-sourced range and register it as an available `CTFSpareRange` for `event`.

    Mirrors the row shape ``ctf.services.range.spares.provision_event_spares``
    leaves behind, without going through actual CMS provisioning dispatch.
    """
    range_instance = _make_spare_range(event=event, owner=owner, scenario_id=scenario_id or event.scenario_id)
    spare = CTFSpareRange.objects.create(
        event=event,
        range_instance_id=range_instance.pk,
        status=status,
    )
    return spare, range_instance


@pytest.fixture
def event_with_scenario(ctf_event, organizer_user, monkeypatch, settings):
    """CTF event configured to rebuild through the RAES creation path.

    ``team_mode=True`` so ``rich_participant`` can validly join a team/bracket.
    """
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
    source = RaesPackageSource.objects.create(
        scenario_id="ctf-range-recovery-test",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="tests/packs/ctf-range-recovery-test",
        package_version="1.0.0",
        package_digest="sha256:" + "a" * 64,
        conformance_status="passed",
        registered_by=organizer_user,
        owner_principal_uuid=ctf_event.owner_principal_uuid,
        scope_kind="account",
        account_uuid=scope.account_uuid,
    )
    ctf_event.scenario_id = source.scenario_id
    ctf_event.range_config = {"agents_by_os": {}, "ngfw_enabled": False}
    ctf_event.team_mode = True
    ctf_event.team_size_limit = 4
    ctf_event.save(update_fields=["scenario_id", "range_config", "team_mode", "team_size_limit"])
    return ctf_event


@pytest.fixture
def team_and_bracket(event_with_scenario):
    bracket = CTFBracket.objects.create(event=event_with_scenario, name="Beginner")
    team = CTFTeam.objects.create(event=event_with_scenario, name="Team Alpha")
    return team, bracket


@pytest.fixture
def rich_participant(event_with_scenario, participant_user, team_and_bracket):
    """A participant with team/bracket/submission/award/cached-score state."""
    team, bracket = team_and_bracket
    participant = CTFParticipant.objects.create(
        event=event_with_scenario,
        user=participant_user,
        email=participant_user.email,
        name="Rich Participant",
        team=team,
        bracket=bracket,
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
        cached_score=250,
        cached_solve_count=2,
    )
    old_range = _make_spare_range(
        event=event_with_scenario,
        owner=participant_user,
        scenario_id=event_with_scenario.scenario_id,
    )
    participant.range_instance_id = old_range.pk
    participant.range_status = ResourceStatus.READY.value
    participant.save(update_fields=["range_instance_id", "range_status", "updated_at"])
    return participant, old_range


class TestReassignSpareRecovery:
    """``strategy=reassign_spare``: consume an event-scoped pooled spare."""

    @pytest.mark.django_db
    def test_personal_scope_event_has_no_separate_egress_policy(self, event_with_scenario, organizer_user):
        from ctf.bridges import cms_range_egress_compatible_with_event

        _spare, spare_range = _make_pooled_spare(event_with_scenario, owner=organizer_user)

        from workspaces.services import resource_scope_from_ids

        scope = resource_scope_from_ids(
            kind=event_with_scenario.scope_kind,
            account_id=event_with_scenario.account_id,
            organization_id=event_with_scenario.organization_id,
            workspace_id=event_with_scenario.workspace_id,
        )
        assert cms_range_egress_compatible_with_event(spare_range.pk, scope)

    @pytest.mark.django_db
    def test_direct_account_event_has_no_workspace_egress_recheck_during_claim(
        self, event_with_scenario, rich_participant, organizer_user, organizer_credential
    ):
        participant, old_range = rich_participant
        spare, spare_range = _make_pooled_spare(event_with_scenario, owner=organizer_user)
        assert event_with_scenario.workspace_id is None

        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        old_range.refresh_from_db()
        spare.refresh_from_db()
        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert old_range.status == ResourceStatus.DESTROYING.value
        assert spare.consumed_by_id == participant.pk

    @pytest.mark.django_db
    def test_reassign_spare_transfers_access_and_blocks_old_range(
        self, event_with_scenario, rich_participant, organizer_user, organizer_credential
    ):
        participant, old_range = rich_participant
        spare, spare_range = _make_pooled_spare(event_with_scenario, owner=organizer_user)

        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert result["replacement_range_instance_id"] == spare_range.pk

        participant.refresh_from_db()
        assert participant.range_instance_id == spare_range.pk

        spare_instance = RangeInstance.objects.get(pk=spare_range.pk)
        assert spare_instance.user_id == organizer_user.pk
        assert spare_instance.owner_principal_uuid == participant.principal_uuid

        spare_engine_range = EngineRange.objects.get(pk=spare_range.engine_range.pk)
        assert spare_engine_range.user_id == organizer_user.pk
        assert spare_engine_range.owner_principal_uuid == participant.principal_uuid

        # The legacy user projection remains historical; principal ownership is
        # the live access key after the hard cut.
        assert EngineRange.resolve_active_for_instance(participant.user, spare_range.instance_uuid) is None

        # Old range is blocked for the (still the same) participant user.
        assert EngineRange.resolve_active_for_instance(participant.user, old_range.instance_uuid) is None

        spare.refresh_from_db()
        assert spare.status == SpareRangeStatus.CONSUMED.value
        assert spare.consumed_by_id == participant.pk
        assert spare.consumed_at is not None

        recovery = CTFRangeRecovery.objects.get(participant=participant, strategy=RecoveryStrategy.REASSIGN_SPARE.value)
        assert recovery.replacement_request_id is None

    @pytest.mark.django_db
    def test_reassign_spare_uses_live_status_when_local_status_stale(
        self, event_with_scenario, rich_participant, second_participant_user, organizer_user, organizer_credential
    ):
        """A spare still locally ``provisioning`` is still usable once CMS reports it READY."""
        participant, _old_range = rich_participant
        _spare, spare_range = _make_pooled_spare(
            event_with_scenario,
            owner=second_participant_user,
            status=SpareRangeStatus.PROVISIONING.value,
        )
        # The underlying RangeInstance was created with status=READY (see
        # _make_spare_range) even though the pool row hasn't been synced yet.

        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert result["replacement_range_instance_id"] == spare_range.pk

    @pytest.mark.django_db
    def test_reassign_spare_excludes_already_consumed_spare(
        self, event_with_scenario, rich_participant, second_participant_user, organizer_user, organizer_credential
    ):
        """A spare already consumed by another participant is never a candidate again."""
        participant, _old_range = rich_participant
        other_participant = CTFParticipant.objects.create(
            event=event_with_scenario,
            user=second_participant_user,
            email=second_participant_user.email,
            name="Other Participant",
            status=ParticipantStatus.ACTIVE.value,
            registered_at=timezone.now(),
        )
        spare, _spare_range = _make_pooled_spare(event_with_scenario, owner=second_participant_user)
        spare.consumed_by = other_participant
        spare.consumed_at = timezone.now()
        spare.status = SpareRangeStatus.CONSUMED.value
        spare.save(update_fields=["consumed_by", "consumed_at", "status", "updated_at"])

        with pytest.raises(CTFRangeError, match="No compatible spare"):
            recover_participant_range(
                participant.pk,
                strategy=RecoveryStrategy.REASSIGN_SPARE.value,
                operator=organizer_credential,
            )

        recovery = CTFRangeRecovery.objects.get(participant=participant, strategy=RecoveryStrategy.REASSIGN_SPARE.value)
        assert recovery.phase == RecoveryPhase.FAILED.value
        assert recovery.failure_category == RecoveryFailureCategory.NO_COMPATIBLE_SPARE.value

    @pytest.mark.django_db
    def test_reserve_before_teardown_leaves_old_range_intact_when_pool_exhausted(
        self, event_with_scenario, rich_participant, second_participant_user, organizer_user, organizer_credential
    ):
        """No-stranding (#307 review): when a competing recovery has taken the only
        spare, this recovery fails at reservation BEFORE tearing down the old
        range, so the participant keeps their old range rather than being left
        with neither. The atomic claim (not a query-only check) is what closes
        the consume-between-availability-and-attachment race."""
        participant, old_range = rich_participant
        competitor = CTFParticipant.objects.create(
            event=event_with_scenario,
            user=second_participant_user,
            email=second_participant_user.email,
            name="Competing Participant",
            status=ParticipantStatus.ACTIVE.value,
            registered_at=timezone.now(),
        )
        # The only spare is already claimed by the competing recovery.
        spare, _spare_range = _make_pooled_spare(event_with_scenario, owner=second_participant_user)
        spare.consumed_by = competitor
        spare.consumed_at = timezone.now()
        spare.status = SpareRangeStatus.CONSUMED.value
        spare.save(update_fields=["consumed_by", "consumed_at", "status", "updated_at"])

        with pytest.raises(CTFRangeError, match="No compatible spare"):
            recover_participant_range(
                participant.pk,
                strategy=RecoveryStrategy.REASSIGN_SPARE.value,
                operator=organizer_credential,
            )

        # Reservation failed before teardown: the old range is neither destroying
        # nor destroyed, and is still active for the participant.
        old_range.refresh_from_db()
        assert old_range.deleted_at is None
        assert old_range.status not in {ResourceStatus.DESTROYING.value, ResourceStatus.DESTROYED.value}
        assert EngineRange.resolve_active_for_instance(participant.user, old_range.instance_uuid) is not None

    @pytest.mark.django_db
    def test_spare_claim_rolls_back_atomically_on_failure(self, event_with_scenario, second_participant_user):
        """The spare claim and the recovery-pointer write commit as one unit (#307
        review): a failure after the claim rolls it back, so a spare is never left
        CONSUMED with no durable pointer -- no pool-capacity leak, and a retry
        re-claims cleanly rather than orphaning the first spare."""
        from django.db import transaction

        from ctf.services.range.recovery_steps import _claim_spare

        participant = CTFParticipant.objects.create(
            event=event_with_scenario,
            user=second_participant_user,
            email=second_participant_user.email,
            name="Claiming Participant",
            status=ParticipantStatus.ACTIVE.value,
            registered_at=timezone.now(),
        )
        spare, _spare_range = _make_pooled_spare(event_with_scenario, owner=event_with_scenario.created_by)

        class _SimulatedCrash(Exception):
            pass

        # _claim_spare must run inside the caller's transaction; _ensure_spare_reserved
        # wraps the claim + pointer write together, so a crash in that window rolls
        # the claim back. Simulate the crash right after the claim. The claim and the
        # crash live in a nested helper so the raises-block holds a single invocation
        # (Sonar S5778) while both still run inside transaction.atomic().
        def _claim_then_crash():
            claimed = _claim_spare(participant, spare.range_instance_id)
            assert claimed is not None
            raise _SimulatedCrash()

        with pytest.raises(_SimulatedCrash), transaction.atomic():
            _claim_then_crash()

        spare.refresh_from_db()
        assert spare.consumed_by_id is None
        assert spare.status == SpareRangeStatus.READY.value

    @pytest.mark.django_db
    def test_reassign_spare_rejects_cross_event_range(
        self, event_with_scenario, rich_participant, second_participant_user, organizer_user, organizer_credential
    ):
        """A same-scenario spare belonging to a DIFFERENT event is never reassignable.

        Regression for the #1018 review finding: spare candidates are
        event-scoped by construction (the ``CTFSpareRange.event`` FK), so an
        organizer cannot pull another event's pooled spare into their own
        participant -- even by naming its id explicitly -- which would
        otherwise be a cross-event range takeover.
        """
        participant, _old_range = rich_participant

        other_event = CTFEvent.objects.create(
            name="Other Event",
            description="A different event sharing the scenario",
            created_by=organizer_user,
            status=EventStatus.REGISTRATION.value,
            event_start=timezone.now() + timedelta(days=1),
            event_end=timezone.now() + timedelta(days=1, hours=8),
            scenario_id=event_with_scenario.scenario_id,
            auto_cleanup=True,
            cleanup_delay_hours=24,
            team_mode=False,
        )
        _foreign_spare, foreign_spare_range = _make_pooled_spare(other_event, owner=second_participant_user)

        # Even naming the foreign spare explicitly must be refused.
        with pytest.raises(CTFRangeError, match="No compatible spare"):
            recover_participant_range(
                participant.pk,
                strategy=RecoveryStrategy.REASSIGN_SPARE.value,
                operator=organizer_credential,
                spare_range_instance_id=foreign_spare_range.pk,
            )

        # The foreign range's ownership is untouched.
        foreign_spare_range.refresh_from_db()
        assert foreign_spare_range.user_id == second_participant_user.id


class TestReassignSpareVpnCustody:
    """A spare's OpenVPN binding moves only when its profile never left custody (#2030)."""

    @pytest.mark.django_db
    def test_vpn_bound_spare_held_by_an_interactive_owner_is_rejected_before_old_range_teardown(
        self, event_with_scenario, rich_participant, second_participant_user, organizer_user, organizer_credential
    ):
        participant, old_range = rich_participant
        spare, spare_range = _make_pooled_spare(event_with_scenario, owner=second_participant_user)
        spare_engine_range = EngineRange.objects.get(pk=spare_range.engine_range.pk)
        spare_engine_range.vpn_access_binding = {"generation": str(spare_range.request.request_id)}
        spare_engine_range.save(update_fields=["vpn_access_binding"])

        with pytest.raises(CTFRangeError, match="No compatible spare"):
            recover_participant_range(
                participant.pk,
                strategy=RecoveryStrategy.REASSIGN_SPARE.value,
                operator=organizer_credential,
                spare_range_instance_id=spare_range.pk,
            )

        old_range.refresh_from_db()
        spare.refresh_from_db()
        assert old_range.deleted_at is None
        assert old_range.status == ResourceStatus.READY.value
        assert EngineRange.resolve_active_for_instance(participant.user, old_range.instance_uuid) is not None
        assert spare.status == SpareRangeStatus.READY.value
        assert spare.consumed_by_id is None

    @pytest.mark.django_db
    def test_vpn_bound_event_spare_moves_its_binding_to_the_participant(
        self, event_with_scenario, rich_participant, organizer_user, organizer_credential
    ):
        """An event-owned spare has no login, so its profile never left custody (#2030)."""
        participant, _old_range = rich_participant
        spare, spare_range = _make_pooled_spare(event_with_scenario, owner=organizer_user)
        spare_engine_range = EngineRange.objects.get(pk=spare_range.engine_range.pk)
        from shared.remote_access import bind_openvpn_realization

        spare_engine_range.vpn_access_binding = bind_openvpn_realization(
            {
                "generation": str(spare_range.request.request_id),
                "target_ref": "provision.node.kali#0",
                "endpoint": "vpn.example.test",
                "port": 1194,
                "secret_ref": "arn:aws:secretsmanager:eu-central-1:123:secret:range-vpn",
            },
            owner_principal_uuid=spare_engine_range.owner_principal_uuid,
        )
        spare_engine_range.save(update_fields=["vpn_access_binding"])

        recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        spare_engine_range.refresh_from_db()
        spare.refresh_from_db()
        assert spare_engine_range.user_id == organizer_user.pk
        assert spare_engine_range.owner_principal_uuid == participant.principal_uuid
        assert spare_engine_range.vpn_access_binding["owner_principal_uuid"] == str(participant.principal_uuid)
        assert spare.consumed_by_id == participant.pk


class TestRebuildFallbackWhenPoolEmpty:
    """An operator can retry with ``rebuild`` after ``reassign_spare`` fails on an empty pool."""

    @pytest.mark.django_db
    def test_rebuild_succeeds_after_reassign_spare_fails_with_empty_pool(
        self, event_with_scenario, rich_participant, organizer_user, organizer_credential
    ):
        participant, old_range = rich_participant
        assert not CTFSpareRange.objects.filter(event=event_with_scenario).exists()

        with pytest.raises(CTFRangeError, match="No compatible spare"):
            recover_participant_range(
                participant.pk,
                strategy=RecoveryStrategy.REASSIGN_SPARE.value,
                operator=organizer_credential,
            )

        failed_recovery = CTFRangeRecovery.objects.get(
            participant=participant, strategy=RecoveryStrategy.REASSIGN_SPARE.value
        )
        assert failed_recovery.phase == RecoveryPhase.FAILED.value

        # A distinct intent record (strategy is part of the unique key) lets the
        # operator retry the SAME old range with a different strategy.
        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REBUILD.value,
            operator=organizer_credential,
        )

        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert result["replacement_range_instance_id"] is not None
        assert result["replacement_range_instance_id"] != old_range.pk

        rebuild_recovery = CTFRangeRecovery.objects.get(
            participant=participant, strategy=RecoveryStrategy.REBUILD.value
        )
        assert rebuild_recovery.phase == RecoveryPhase.COMPLETED.value
        assert CTFRangeRecovery.objects.filter(participant=participant).count() == 2

        participant.refresh_from_db()
        assert participant.range_instance_id == result["replacement_range_instance_id"]


class TestIdempotentRetry:
    """Recovery queues durable teardown and repeated requests do not duplicate it."""

    @pytest.mark.django_db
    def test_teardown_is_queued_without_inline_cloud_execution(
        self,
        monkeypatch,
        event_with_scenario,
        rich_participant,
        second_participant_user,
        organizer_user,
        organizer_credential,
    ):
        participant, old_range = rich_participant
        spare, spare_range = _make_pooled_spare(event_with_scenario, owner=second_participant_user)
        called = {"value": False}

        def fail_teardown(_request_id):
            called["value"] = True
            raise CloudTaskError("simulated transient teardown failure")

        monkeypatch.setattr("engine.ecs.start_range_teardown", fail_teardown)
        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        old_range.refresh_from_db()
        spare.refresh_from_db()
        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert not called["value"]
        assert old_range.status == ResourceStatus.DESTROYING.value
        assert spare.consumed_by_id == participant.pk
        from engine.models import ProvisionerLaunchIntent

        assert ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").count() == 1

    @pytest.mark.django_db
    def test_completed_recovery_records_one_teardown_and_audit(
        self,
        monkeypatch,
        event_with_scenario,
        rich_participant,
        second_participant_user,
        organizer_user,
        organizer_credential,
    ):
        participant, old_range = rich_participant
        _spare, spare_range = _make_pooled_spare(event_with_scenario, owner=second_participant_user)
        call_count = {"n": 0}

        def flaky_teardown(request_id):
            call_count["n"] += 1
            raise CloudTaskError("cloud execution must be deferred")

        monkeypatch.setattr("engine.ecs.start_range_teardown", flaky_teardown)

        result = recover_participant_range(
            participant.pk,
            strategy=RecoveryStrategy.REASSIGN_SPARE.value,
            operator=organizer_credential,
            spare_range_instance_id=spare_range.pk,
        )

        assert result["phase"] == RecoveryPhase.COMPLETED.value
        assert result["replacement_range_instance_id"] == spare_range.pk
        assert call_count["n"] == 0
        # No duplicate recovery record or audit row from the retry.
        assert CTFRangeRecovery.objects.filter(participant=participant).count() == 1
        assert AuditLog.objects.filter(action=AuditAction.RECOVER, entity_id=old_range.pk).count() == 1
        from engine.models import ProvisionerLaunchIntent

        assert ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").count() == 1

        participant.refresh_from_db()
        assert participant.range_instance_id == spare_range.pk
