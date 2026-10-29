"""Cancellation serializes with native spare reservation and pointer binding."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep

import pytest
from django.db import connection

from cms.exceptions import CMSError
from cms.models import RangeInstance
from ctf.enums import RecoveryFailureCategory
from ctf.exceptions import CTFRangeError
from ctf.models import CTFRangeLaunch, CTFSpareRange
from ctf.services.range.launch_intents import cancel_event_launches
from ctf.services.range.provision import provision_participant_range
from ctf.services.range.recovery import _claim_recovery, recover_participant_range
from ctf.services.range.recovery_steps import _ensure_participant_repointed, _ensure_spare_reserved
from ctf.services.range.spares import provision_event_spares
from engine.models import ProvisionerLaunchIntent, Range
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event


def ready_pool(event, participant, credential):
    """Create both generations through their real owning launch commands."""
    provision_participant_range(participant.pk, actor=credential)
    participant.refresh_from_db()
    old = RangeInstance.objects.get(pk=participant.range_instance_id)
    provision_event_spares(event.pk, 1, operator=credential)
    spare = CTFSpareRange.objects.get(event=event)
    RangeInstance.objects.filter(pk=spare.range_instance_id).update(status="ready")
    Range.objects.filter(request__request_id=spare.request_id).update(status="ready")
    spare.status = "ready"
    spare.save(update_fields=["status", "updated_at"])
    return old, spare


def assert_old_untouched(participant, old):
    participant.refresh_from_db()
    old.refresh_from_db()
    assert participant.range_instance_id == old.pk
    assert participant.range_request_id == old.request.request_id
    assert old.status != "destroying"
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()


@pytest.mark.django_db(transaction=True)
def test_cancelled_spare_is_rejected_before_old_range_teardown(launch_event, ctf_participant, organizer_credential):
    old, spare = ready_pool(launch_event, ctf_participant, organizer_credential)
    cancel_event_launches(launch_event.pk, organizer_credential, spares_only=True)

    with pytest.raises(CTFRangeError) as error:
        recover_participant_range(
            ctf_participant.pk,
            strategy="reassign_spare",
            operator=organizer_credential,
            spare_range_instance_id=spare.range_instance_id,
        )

    assert error.value.details["failure_category"] == RecoveryFailureCategory.NO_COMPATIBLE_SPARE.value
    spare.refresh_from_db()
    assert spare.consumed_by_id is None
    assert_old_untouched(ctf_participant, old)


@pytest.mark.django_db(transaction=True)
def test_cancelled_reserved_spare_cannot_resume_or_repoint(launch_event, ctf_participant, organizer_credential):
    old, spare = ready_pool(launch_event, ctf_participant, organizer_credential)
    participant, recovery, _ = _claim_recovery(ctf_participant.pk, "reassign_spare", organizer_credential)
    _ensure_spare_reserved(recovery, participant, spare.range_instance_id)
    spare.refresh_from_db()
    assert spare.consumed_by_id == participant.pk
    assert recovery.replacement_request_id == spare.request_id
    cancel_event_launches(launch_event.pk, organizer_credential)

    with pytest.raises(CTFRangeError, match="No compatible spare"):
        _ensure_spare_reserved(recovery, participant, spare.range_instance_id)
    with pytest.raises(CTFRangeError, match="cancelled"):
        _ensure_participant_repointed(participant, recovery)
    assert_old_untouched(participant, old)


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("phase", ["claim", "owner_move"])
def test_spare_claim_waits_for_event_cancellation_commit(
    launch_event, ctf_participant, organizer_credential, phase, record_property
):
    """Observe actual PostgreSQL blocking between two owning commands."""
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row-lock contention")
    old, spare = ready_pool(launch_event, ctf_participant, organizer_credential)
    if phase == "owner_move":
        participant, recovery, _ = _claim_recovery(ctf_participant.pk, "reassign_spare", organizer_credential)
        _ensure_spare_reserved(recovery, participant, spare.range_instance_id)
    instance = RangeInstance.objects.get(pk=spare.range_instance_id)
    runtime = Range.objects.get(request__request_id=spare.request_id)
    previous_owners = (instance.owner_principal_uuid, runtime.owner_principal_uuid)
    cancelled, resume, recovery_started = Event(), Event(), Event()
    backend_ids = {}

    def setup_connection(name):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '8s'")
            cursor.execute("SET statement_timeout = '12s'")
            cursor.execute("SELECT pg_backend_pid()")
            backend_ids[name] = cursor.fetchone()[0]

    def cancel():
        def pause(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith('UPDATE "ctf_ctfrangelaunch"'):
                cancelled.set()
                assert resume.wait(15), "Cancellation was not released"
            return result

        try:
            setup_connection("canceller")
            with connection.execute_wrapper(pause):
                return cancel_event_launches(launch_event.pk, organizer_credential, spares_only=phase == "claim")
        finally:
            connection.close()

    def recover():
        try:
            setup_connection("recovery")
            recovery_started.set()
            expected_error = CTFRangeError if phase == "claim" else CMSError
            with pytest.raises(expected_error) as error:
                if phase == "claim":
                    recover_participant_range(
                        ctf_participant.pk,
                        strategy="reassign_spare",
                        operator=organizer_credential,
                        spare_range_instance_id=spare.range_instance_id,
                    )
                else:
                    from ctf.bridges import cms_reassign_range_owner
                    from management.services import resolve_principal_uuid

                    cms_reassign_range_owner(
                        organizer_credential,
                        spare.range_instance_id,
                        resolve_principal_uuid(ctf_participant.principal_uuid),
                        launch_event.pk,
                    )
            return error.value
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelling = pool.submit(cancel)
        try:
            assert cancelled.wait(10), "Cancellation did not acquire its event mutex"
            recovering = pool.submit(recover)
            assert recovery_started.wait(10), "Recovery did not open its database connection"
            deadline = monotonic() + 5
            blocked = False
            while monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_blocking_pids(pid), wait_event_type, query FROM pg_stat_activity WHERE pid = %s",
                        [backend_ids["recovery"]],
                    )
                    blockers, wait_type, blocked_query = cursor.fetchone()
                    blocked = backend_ids["canceller"] in blockers
                if blocked:
                    break
                sleep(0.02)
            assert blocked, "Recovery did not block on the cancellation transaction"
            assert wait_type == "Lock"
            # A fresh recovery can first block at COMMIT: its deferred event FK
            # needs KEY SHARE against the canceller's Event FOR UPDATE. A retry
            # reaches the explicit event mutex instead. Both are real database
            # serialization; neither is inferred from elapsed time or one SQL shape.
            record_property("cancellation_blocked_query", blocked_query)
        finally:
            resume.set()
        cancelling.result(timeout=20)
        failure = recovering.result(timeout=20)
    if phase == "claim":
        assert failure.details["failure_category"] == RecoveryFailureCategory.NO_COMPATIBLE_SPARE.value
    else:
        instance.refresh_from_db()
        runtime.refresh_from_db()
        assert (instance.owner_principal_uuid, runtime.owner_principal_uuid) == previous_owners
    assert not CTFRangeLaunch.objects.filter(pk=spare.request_id).exists()
    spare.refresh_from_db()
    assert spare.consumed_by_id == (None if phase == "claim" else ctf_participant.pk)
    assert_old_untouched(ctf_participant, old)
