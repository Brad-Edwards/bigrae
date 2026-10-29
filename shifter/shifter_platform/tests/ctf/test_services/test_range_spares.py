"""Principal-native CTF spare-pool coverage for the identity hard cut."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cms.models import RangeInstance
from ctf.enums import SpareRangeStatus
from ctf.exceptions import CTFNotFoundError, CTFPermissionError
from ctf.models import CTFEvent, CTFSpareRange
from ctf.services.range.spares import cleanup_event_spares, get_event_spare_summary, provision_event_spares
from engine.models import Range
from shared.audit import AuditAction
from shared.models import AuditLog
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event


@pytest.fixture
def event_with_scenario(launch_event) -> CTFEvent:
    """Use the real synthetic package, compiler, CMS port and native outbox."""
    return launch_event


@pytest.mark.django_db
def test_spares_are_event_principal_owned_without_placeholder_users(
    event_with_scenario, organizer_user, organizer_credential
):
    from django.contrib.auth import get_user_model

    before_users = get_user_model().objects.count()
    result = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)

    assert result == {
        "event_id": str(event_with_scenario.pk),
        "target_count": 2,
        "existing": 0,
        "created": 2,
    }
    assert get_user_model().objects.count() == before_users
    spares = list(CTFSpareRange.objects.filter(event=event_with_scenario))
    assert len(spares) == 2
    for spare in spares:
        instance = RangeInstance.objects.get(pk=spare.range_instance_id)
        runtime = Range.objects.get(request__request_id=instance.request.request_id)
        assert instance.user_id == organizer_user.pk
        assert runtime.user_id == organizer_user.pk
        assert instance.owner_principal_uuid == event_with_scenario.owner_principal_uuid
        assert runtime.owner_principal_uuid == event_with_scenario.owner_principal_uuid
        assert instance.event_uuid == event_with_scenario.pk
        assert runtime.event_uuid == event_with_scenario.pk
        assert runtime.egress_mode == "none"

    audit = AuditLog.objects.get(action=AuditAction.SPARE_PROVISION)
    assert audit.actor_id is None
    assert audit.actor_principal_uuid == event_with_scenario.owner_principal_uuid
    assert audit.new_state["created"] == 2


@pytest.mark.django_db
def test_spare_top_up_is_idempotent_and_replaces_terminal_rows(event_with_scenario, organizer_credential):
    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    first = set(CTFSpareRange.objects.values_list("pk", flat=True))
    same = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    assert same["existing"] == 2 and same["created"] == 0
    assert set(CTFSpareRange.objects.values_list("pk", flat=True)) == first

    CTFSpareRange.objects.filter(pk=next(iter(first))).update(status=SpareRangeStatus.FAILED.value)
    replaced = provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    assert replaced["existing"] == 1 and replaced["created"] == 1
    assert CTFSpareRange.objects.count() == 3


@pytest.mark.django_db
def test_spare_provision_requires_live_event_range_authority(event_with_scenario, participant_user):
    with pytest.raises(CTFPermissionError):
        provision_event_spares(event_with_scenario.pk, 1, operator=participant_user)
    assert not CTFSpareRange.objects.exists()


@pytest.mark.django_db
def test_spare_provision_requires_an_initiating_credential(event_with_scenario):
    with pytest.raises(ValueError, match="authority unavailable"):
        provision_event_spares(event_with_scenario.pk, 1)
    assert not CTFSpareRange.objects.exists()


@pytest.mark.django_db
def test_spare_provision_failure_is_recorded(ctf_event, organizer_credential):
    from ctf.models import CTFRangeLaunch

    result = provision_event_spares(ctf_event.pk, 2, operator=organizer_credential)
    assert result["created"] == 2
    assert set(CTFSpareRange.objects.values_list("status", flat=True)) == {SpareRangeStatus.PROVISIONING.value}
    assert CTFRangeLaunch.objects.filter(dispatched_at=None).count() == 2
    first = set(CTFRangeLaunch.objects.values_list("pk", flat=True))
    provision_event_spares(ctf_event.pk, 2, operator=organizer_credential)
    assert set(CTFRangeLaunch.objects.values_list("pk", flat=True)) == first


@pytest.mark.django_db
def test_spare_summary_counts_statuses(event_with_scenario, organizer_credential):
    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    rows = list(CTFSpareRange.objects.order_by("pk"))
    rows[0].status = SpareRangeStatus.READY.value
    rows[0].save(update_fields=["status", "updated_at"])
    summary = get_event_spare_summary(event_with_scenario.pk)
    assert summary["target_count"] == 2
    assert summary["counts"][SpareRangeStatus.READY.value] == 1
    assert summary["counts"][SpareRangeStatus.PROVISIONING.value] == 1
    assert summary["available"] == 1


@pytest.mark.django_db
def test_cleanup_dispatches_with_operator_credential(event_with_scenario, organizer_credential):
    from engine.models import RangeCancellationAuthority

    provision_event_spares(event_with_scenario.pk, 2, operator=organizer_credential)
    result = cleanup_event_spares(event_with_scenario.pk, actor=organizer_credential)
    assert result == {"event_id": str(event_with_scenario.pk), "destroyed": 2, "failed": 0}
    cleanups = RangeCancellationAuthority.objects.filter(authority_kind="cleanup")
    assert cleanups.count() == 2
    assert all(row.initiating_principal_uuid == organizer_credential.principal.uuid for row in cleanups)
    assert all(row.credential_uuid == organizer_credential.credential_uuid for row in cleanups)
    assert set(CTFSpareRange.objects.values_list("status", flat=True)) == {SpareRangeStatus.PROVISIONING.value}
    assert set(RangeInstance.all_objects.values_list("status", flat=True)) == {"destroying"}


@pytest.mark.django_db
def test_cleanup_preserves_retryable_row_after_dispatch_failure(event_with_scenario, organizer_credential):
    from django.db import connection

    from engine.models import RangeCancellationAuthority

    provision_event_spares(event_with_scenario.pk, 1, operator=organizer_credential)

    def fail(execute, sql, params, many, context):
        if sql.startswith('INSERT INTO "engine_range_cancellation_authority"'):
            raise RuntimeError("Synthetic cleanup commit failure")
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail):
        result = cleanup_event_spares(event_with_scenario.pk, actor=organizer_credential)
    assert result["destroyed"] == 0 and result["failed"] == 1
    assert CTFSpareRange.objects.get().status == SpareRangeStatus.PROVISIONING.value
    assert not RangeCancellationAuthority.objects.filter(authority_kind="cleanup").exists()
    retried = cleanup_event_spares(event_with_scenario.pk, actor=organizer_credential)
    assert retried["destroyed"] == 1 and retried["failed"] == 0
    assert RangeCancellationAuthority.objects.filter(authority_kind="cleanup").count() == 1


@pytest.mark.django_db
def test_unknown_event_is_not_found(organizer_user):
    missing = uuid4()
    with pytest.raises(CTFNotFoundError):
        provision_event_spares(missing, 1, operator=organizer_user)
    with pytest.raises(CTFNotFoundError):
        cleanup_event_spares(missing, actor=organizer_user)
