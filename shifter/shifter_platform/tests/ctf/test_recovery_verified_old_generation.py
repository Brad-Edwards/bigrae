"""Recovery resumes after exact provider-verified old-generation teardown."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection

from cms.models import RangeInstance
from ctf.exceptions import CTFRangeError
from ctf.models import CTFRangeRecovery
from ctf.services.range.recovery import recover_participant_range
from engine.models import ProvisionerLaunchIntent, Range
from tests.cms.conftest import make_pack as make_pack
from tests.ctf.test_range_launch_crash_recovery import ProcessCrash
from tests.ctf.test_range_launch_crash_recovery import launch_event as launch_event
from tests.ctf.test_spare_claim_cancellation import ready_pool
from tests.engine.services.test_native_warm_retirement import _apply_result, _cleanup_provider

pytestmark = pytest.mark.django_db(transaction=True)


def paused_recovery(event, participant, credential, strategy):
    old, spare = ready_pool(event, participant, credential)
    Range.objects.filter(request__request_id=old.request.request_id).update(status="ready")
    RangeInstance.objects.filter(pk=old.pk).update(status="ready")

    def crash_after_teardown(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith('UPDATE "ctf_ctfrangerecovery"') and "old_range_blocked" in params:
            raise ProcessCrash("after durable old teardown, before replacement attachment")
        return result

    with connection.execute_wrapper(crash_after_teardown), pytest.raises(ProcessCrash):
        recover_participant_range(participant.pk, strategy=strategy, operator=credential)
    recovery = CTFRangeRecovery.objects.get(participant=participant)
    cleanup = ProvisionerLaunchIntent.objects.get(
        payload__request_id=str(old.request.request_id), payload__operation="destroy"
    )
    return old, spare, recovery, cleanup


def apply_verified_old_cleanup(old, cleanup):
    from shared.operation_results import ResultStep

    runtime = Range.objects.get(request__request_id=old.request.request_id)
    case = SimpleNamespace(request_id=old.request.request_id, runtime=runtime)
    provider_module, scope, provider = _cleanup_provider(case)
    evidence = provider_module.destroy_ec2_resources(scope, provider)
    assert evidence["outcome"] == "VERIFIED_ABSENT"
    _apply_result(
        cleanup, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": evidence}
    )
    from cms.handlers import process_range_event
    from engine.models import RangeEventOutbox

    status_event = RangeEventOutbox.objects.get(
        payload__request_id=str(old.request.request_id), payload__new_status="destroyed"
    )
    process_range_event(status_event.payload)


@pytest.mark.parametrize("strategy", ["reassign_spare", "rebuild"])
def test_recovery_resumes_after_verified_old_pointer_clear(
    launch_event, ctf_participant, organizer_credential, strategy
):
    from cms.services import is_event_range_cleanup_verified_absent
    from ctf.models import CTFRangeLaunch

    old, _spare, recovery, cleanup = paused_recovery(launch_event, ctf_participant, organizer_credential, strategy)
    apply_verified_old_cleanup(old, cleanup)
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id is None
    assert ctf_participant.range_request_id == old.request.request_id
    assert is_event_range_cleanup_verified_absent(launch_event.pk, old.pk, old.request.request_id)

    result = recover_participant_range(ctf_participant.pk, strategy=strategy, operator=organizer_credential)
    assert result["phase"] == "completed"
    ctf_participant.refresh_from_db()
    recovery.refresh_from_db()
    assert ctf_participant.range_instance_id == recovery.replacement_range_instance_id
    assert ctf_participant.range_request_id == recovery.replacement_request_id
    assert ctf_participant.range_instance_id != old.pk
    if strategy == "rebuild":
        intent = CTFRangeLaunch.objects.get(pk=recovery.replacement_request_id)
        # Teardown cannot turn the old range's sharing subject back into a draw.
        assert intent.parameters["model_subject"] == {
            "owner": "deployment",
            "reference": f"range:{Range.objects.get(request__request_id=old.request.request_id).uuid}",
        }


@pytest.mark.parametrize("proof", ["logical_only", "wrong_operation"])
def test_pointer_clear_requires_exact_accepted_cleanup_evidence(
    launch_event, ctf_participant, organizer_credential, proof
):
    from django.utils import timezone

    from ctf.models import CTFParticipant
    from engine.services import record_cleanup_verification

    old, _spare, recovery, _cleanup = paused_recovery(
        launch_event, ctf_participant, organizer_credential, "reassign_spare"
    )
    Range.objects.filter(request__request_id=old.request.request_id).update(status="destroyed")
    RangeInstance.all_objects.filter(pk=old.pk).update(status="destroyed")
    CTFParticipant.objects.filter(pk=ctf_participant.pk).update(range_instance_id=None, range_status="")
    if proof == "wrong_operation":
        record_cleanup_verification(
            request_id=old.request.request_id,
            operation_id=uuid4(),
            outcome="VERIFIED_ABSENT",
            scope={"provider": "synthetic"},
            residual_categories=[],
            observed_at=timezone.now(),
        )
    with pytest.raises(CTFRangeError, match="no range assigned"):
        recover_participant_range(ctf_participant.pk, strategy="reassign_spare", operator=organizer_credential)
    ctf_participant.refresh_from_db()
    assert ctf_participant.range_instance_id is None
    assert ctf_participant.range_request_id == old.request.request_id
    assert CTFRangeRecovery.objects.filter(pk=recovery.pk).exists()


@pytest.mark.parametrize("change", ["cancelled", "reassigned"])
def test_verified_old_cleanup_cannot_override_cancellation_or_new_assignment(
    launch_event,
    ctf_participant,
    organizer_credential,
    change,
):
    from ctf.models import CTFParticipant
    from ctf.services.range.launch_intents import cancel_event_launches
    from ctf.services.range.recovery_steps import _ensure_participant_repointed

    old, spare, recovery, cleanup = paused_recovery(
        launch_event, ctf_participant, organizer_credential, "reassign_spare"
    )
    apply_verified_old_cleanup(old, cleanup)
    if change == "cancelled":
        cancel_event_launches(launch_event.pk, organizer_credential)
    else:
        CTFParticipant.objects.filter(pk=ctf_participant.pk).update(
            range_instance_id=spare.range_instance_id, range_request_id=uuid4()
        )
    before = CTFParticipant.objects.values_list("range_instance_id", "range_request_id").get(pk=ctf_participant.pk)
    with pytest.raises(CTFRangeError, match="changed or was cancelled"):
        _ensure_participant_repointed(ctf_participant, recovery)
    assert (
        CTFParticipant.objects.values_list("range_instance_id", "range_request_id").get(pk=ctf_participant.pk) == before
    )
