"""Native cleanup preserves current generations and unresolved capacity."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.utils import timezone

from cms.models import RangeInstance
from ctf.models import CTFEvent, CTFRangeLaunch, CTFSpareRange
from ctf.services.range.lifecycle import cleanup_event_ranges
from ctf.services.range.provision import provision_participant_range
from ctf.services.range.spares import provision_event_spares
from engine.models import CapacityDraw, CapacityReservation, ProvisionerLaunchIntent, Range
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import ProcessCrash, crash_at
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event


def capacity_budget(event):
    now = timezone.now()
    return CapacityReservation.objects.create(
        event_ref=event.pk,
        partition_name="synthetic",
        metric_name="ranges",
        amount=10,
        unit_amount=1,
        window_start=now - timedelta(hours=1),
        window_end=now + timedelta(hours=1),
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["participant", "spare"])
def test_force_delete_settles_cancelled_request_before_cms_reservation(
    launch_event,
    ctf_participant,
    organizer_credential,
    kind,
):
    from ctf.services.event import force_delete_event

    capacity_budget(launch_event)
    with connection.execute_wrapper(crash_at("reserve")), pytest.raises(ProcessCrash):
        if kind == "participant":
            provision_participant_range(ctf_participant.pk, actor=organizer_credential)
        else:
            provision_event_spares(launch_event.pk, 1, operator=organizer_credential)
    assert CTFRangeLaunch.objects.exists()
    assert not RangeInstance.objects.exists()
    assert CapacityDraw.objects.filter(released_at=None).count() == 1
    result = force_delete_event(launch_event.pk, organizer_credential, launch_event.name)
    assert result["ranges_failed"] == 0
    assert not CTFEvent.all_objects.filter(pk=launch_event.pk).exists()
    assert not CapacityDraw.objects.filter(released_at=None).exists()
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


def verified_cleanup(instance):
    from engine.services import record_cleanup_verification

    RangeInstance.all_objects.filter(pk=instance.pk).update(status="destroyed")
    record_cleanup_verification(
        request_id=instance.request.request_id,
        operation_id=uuid4(),
        outcome="VERIFIED_ABSENT",
        scope={"provider": "synthetic"},
        residual_categories=[],
        observed_at=timezone.now(),
    )


@pytest.mark.django_db(transaction=True)
def test_logical_destroyed_status_retains_claim_until_verified(
    launch_event,
    ctf_participant,
    organizer_credential,
):
    capacity_budget(launch_event)
    provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    instance = RangeInstance.objects.get()
    RangeInstance.objects.filter(pk=instance.pk).update(status="destroyed")
    cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id == instance.pk
    assert ctf_participant.range_request_id == instance.request.request_id
    assert CapacityDraw.objects.filter(draw_key=ctf_participant.pk, released_at=None).exists()
    verified_cleanup(instance)
    cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id is None
    assert ctf_participant.range_request_id is None
    assert not CapacityDraw.objects.filter(draw_key=ctf_participant.pk, released_at=None).exists()


@pytest.mark.django_db(transaction=True)
def test_cleanup_of_old_launch_preserves_and_destroys_recovered_spare(
    launch_event,
    ctf_participant,
    organizer_credential,
):
    from ctf.services.range.recovery import recover_participant_range

    capacity_budget(launch_event)
    provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    old = RangeInstance.objects.get()
    provision_event_spares(launch_event.pk, 1, operator=organizer_credential)
    spare = CTFSpareRange.objects.get()
    replacement = RangeInstance.objects.get(pk=spare.range_instance_id)
    RangeInstance.objects.filter(pk=replacement.pk).update(status="ready")
    Range.objects.filter(pk=replacement.range_id).update(status="ready")
    CTFSpareRange.objects.filter(pk=spare.pk).update(status="ready")
    result = recover_participant_range(
        ctf_participant.pk,
        strategy="reassign_spare",
        operator=organizer_credential,
        spare_range_instance_id=replacement.pk,
    )
    assert result["phase"] == "completed"
    verified_cleanup(old)
    result = cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
    assert result["failed"] == 0
    ctf_participant.refresh_from_db()
    replacement.refresh_from_db()
    assert ctf_participant.range_instance_id == replacement.pk
    assert ctf_participant.range_request_id == replacement.request.request_id
    assert replacement.status == "destroying"
    assert CapacityDraw.objects.filter(draw_key=ctf_participant.pk, released_at=None).exists()


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_spare_cleanup_cancels_topup_before_first_cms_call(launch_event, organizer_credential):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ctf.services.range.spares import cleanup_event_spares

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock contention")
    capacity_budget(launch_event)
    paused, resume = Event(), Event()

    def top_up():
        inserted = stopped = False

        def stop(execute, sql, params, many, context):
            nonlocal inserted, stopped
            if sql.startswith('INSERT INTO "ctf_ctfsparerange"'):
                inserted = True
            if inserted and not stopped and not connection.in_atomic_block and 'FROM "ctf_event"' in sql:
                stopped = True
                paused.set()
                assert resume.wait(20)
            return execute(sql, params, many, context)

        try:
            with connection.execute_wrapper(stop):
                return provision_event_spares(launch_event.pk, 1, operator=organizer_credential)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        creating = pool.submit(top_up)
        try:
            assert paused.wait(20), creating.result(timeout=1) if creating.done() else "Top-up did not pause"
            spare = CTFSpareRange.objects.get()
            assert spare.request_id is not None
            assert CTFRangeLaunch.objects.filter(pk=spare.request_id).exists()
            assert not RangeInstance.objects.exists()
            cleaned = cleanup_event_spares(launch_event.pk, actor=organizer_credential)
            assert cleaned["failed"] == 0
        finally:
            resume.set()
        creating.result(timeout=25)
    assert CTFSpareRange.objects.get().status == "failed"
    assert not CTFRangeLaunch.objects.exists()
    assert not CapacityDraw.objects.filter(released_at=None).exists()
    assert not RangeInstance.objects.exists()
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_capacity_release_failure_retains_claim_for_retry(launch_event, ctf_participant, organizer_credential):
    capacity_budget(launch_event)
    provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    instance = RangeInstance.objects.get()
    verified_cleanup(instance)

    def fail_release(execute, sql, params, many, context):
        if sql.startswith('UPDATE "engine_capacity_draw"'):
            raise RuntimeError("Synthetic capacity release write failure")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_release):
        result = cleanup_event_ranges(launch_event.pk, actor=organizer_credential)
    assert result["failed"] == 1
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id == instance.pk
    assert ctf_participant.range_request_id == instance.request.request_id
    assert CapacityDraw.objects.filter(draw_key=ctf_participant.pk, released_at=None).exists()
    assert cleanup_event_ranges(launch_event.pk, actor=organizer_credential)["failed"] == 0
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id is None
    assert ctf_participant.range_request_id is None
    assert not CapacityDraw.objects.filter(draw_key=ctf_participant.pk, released_at=None).exists()


@pytest.mark.django_db(transaction=True)
def test_event_cleanup_includes_spare_claimed_by_unfinished_recovery(
    launch_event, ctf_participant, organizer_credential
):
    from ctf.exceptions import CTFRangeError
    from ctf.services.range.recovery import recover_participant_range

    provision_participant_range(ctf_participant.pk, actor=organizer_credential)
    old = RangeInstance.objects.get()
    provision_event_spares(launch_event.pk, 1, operator=organizer_credential)
    spare = CTFSpareRange.objects.get()
    RangeInstance.objects.filter(pk=spare.range_instance_id).update(status="ready")
    Range.objects.filter(request__request_id=spare.request_id).update(status="ready")
    CTFSpareRange.objects.filter(pk=spare.pk).update(status="ready")

    def fail_old_cleanup(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "engine_range_cancellation_authority"'):
            raise RuntimeError("Synthetic failure after durable spare claim")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_old_cleanup), pytest.raises(CTFRangeError):
        recover_participant_range(ctf_participant.pk, strategy="reassign_spare", operator=organizer_credential)
    ctf_participant.refresh_from_db()
    spare.refresh_from_db()
    assert ctf_participant.range_instance_id == old.pk
    assert spare.consumed_by_id == ctf_participant.pk
    assert cleanup_event_ranges(launch_event.pk, actor=organizer_credential)["failed"] == 0
    assert set(RangeInstance.all_objects.values_list("status", flat=True)) == {"destroying"}
    from engine.models import InterruptState, RangeCancellationAuthority

    # The old generation is still provisioning: native cleanup interrupts its
    # incumbent provision intent. The READY spare gets a separate destroy intent.
    old_runtime = Range.objects.get(request__request_id=old.request.request_id)
    provision = ProvisionerLaunchIntent.objects.get(operation_id=old_runtime.provisioner_operation_id)
    assert provision.authority_kind == "principal"
    assert provision.payload["request_id"] == str(old.request.request_id)
    assert provision.payload["operation"] == "provision"
    assert provision.interrupt_state == InterruptState.REQUESTED
    assert provision.interrupt_requested_at is not None
    assert provision.interrupt_next_attempt_at is not None
    assert provision.interrupt_deadline is not None
    cancellation = RangeCancellationAuthority.objects.get(provision_intent=provision)
    assert cancellation.authority_kind == "cleanup"
    assert cancellation.authority_resource_uuid == old.uuid
    assert cancellation.authority_owner_uuid == old_runtime.owner_principal_uuid
    teardown = ProvisionerLaunchIntent.objects.get(authority_kind="cleanup")
    assert teardown.payload["operation"] == "destroy"
    assert teardown.payload["request_id"] == str(spare.request_id)
    assert cleanup_event_ranges(launch_event.pk, actor=organizer_credential)["failed"] == 0
    assert ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup").count() == 1
    assert RangeCancellationAuthority.objects.filter(provision_intent=provision).count() == 1
    with pytest.raises(CTFRangeError):
        recover_participant_range(ctf_participant.pk, strategy="reassign_spare", operator=organizer_credential)
