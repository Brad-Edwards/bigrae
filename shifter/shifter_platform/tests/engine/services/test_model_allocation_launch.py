"""Allocation joins the existing operation-input and dispatch-intent transaction."""

import pytest

from engine.models import ModelAllocation, OperationInput, ProvisionerLaunchIntent, Range
from engine.models import Request as EngineRequest

from .test_model_allocation import allocation_inputs

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("provisioner_workload_config"),
]


def prepared_launch(django_user_model, *, required=True):
    from cms.models import RangeInstance
    from cms.models import Request as CmsRequest
    from engine.services._model_allocation_launch import prepare_model_launch, record_model_observations
    from shared.model_access.reservation import ModelLaunchScope

    from ..test_operation_input_raes import _plan

    catalog, request, observations = allocation_inputs(django_user_model)
    request = request.model_copy(update={"need": request.need.model_copy(update={"required": required})})
    record_model_observations(catalog, lambda _: observations)
    scope = ModelLaunchScope(
        kind=request.scope_kind,
        scope_id=request.scope_id,
        draw_key=request.draw_key,
        subject_ref=request.subject_ref,
        window_start=request.window_start,
        window_end=request.window_end,
        demands=(request.demand,),
    )
    prepare_model_launch(
        request_id=request.request_id,
        owner_ref=request.owner_ref,
        needs=(request.need,),
        scope=scope,
        authority_revisions=request.authority_revisions,
        catalog=catalog,
    )
    Range.objects.filter(uuid=request.range_id).update(
        provisioner_operation_id=None,
        provisioner_operation="",
        range_config=_plan(),
        range_backend="gce",
        instantiation_purpose="training",
    )
    runtime = Range.objects.select_related("request", "user").get(uuid=request.range_id)
    cms_request = CmsRequest.objects.create(
        request_id=request.request_id,
        request_type="range",
        user=runtime.user,
        owner_principal_uuid=runtime.owner_principal_uuid,
        scope_kind=runtime.scope_kind,
        account_id=runtime.account_id,
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        user_id=runtime.user_id,
        owner_principal_uuid=runtime.owner_principal_uuid,
        scope_kind=runtime.scope_kind,
        account_id=runtime.account_id,
        scenario_id="synthetic-model-launch",
        range_source="mission_control",
        status="provisioning",
    )
    runtime.cms_range_uuid = instance.uuid
    runtime.save(update_fields=["cms_range_uuid"])
    instance.range_id = runtime.pk
    instance.save(update_fields=["range_id"])
    return request


def launch_credential(request, live_session_credential):
    """Return the exact live initiator proof for the prepared principal range."""
    owner = Range.objects.select_related("user").get(uuid=request.range_id).user
    return live_session_credential(owner)


def test_launch_persists_grant_and_input_with_actual_operation_id(
    django_user_model, live_session_credential, allow_all_policy
):
    from engine.launch_intents import enqueue_provisioner_launch

    request = prepared_launch(django_user_model)
    enqueue_provisioner_launch(
        ["raes-range", "provision", "--request-id", str(request.request_id)],
        credential=launch_credential(request, live_session_credential),
    )
    intent = ProvisionerLaunchIntent.objects.get()
    allocation = ModelAllocation.objects.get()
    assert allocation.operation_id == intent.operation_id
    assert allocation.grant.state == "pending"
    assert OperationInput.objects.filter(operation_id=intent.operation_id).exists()


def test_input_failure_rolls_back_allocation_and_dispatch_intent(
    django_user_model, monkeypatch, live_session_credential, allow_all_policy
):
    from engine.launch_intents import enqueue_provisioner_launch

    request = prepared_launch(django_user_model)

    def reject_input(*args, **kwargs):
        raise ValueError("input rejected")

    monkeypatch.setattr("engine.launch_intents._materialize_operation_input", reject_input)
    with pytest.raises(ValueError, match="input rejected"):
        enqueue_provisioner_launch(
            ["raes-range", "provision", "--request-id", str(request.request_id)],
            credential=launch_credential(request, live_session_credential),
        )
    assert not ModelAllocation.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


def test_provider_observation_is_forbidden_inside_outer_transaction(django_user_model):
    from django.db import transaction

    from engine.services._model_allocation_launch import record_model_observations
    from shared.model_access import ContractError

    catalog, _, observations = allocation_inputs(django_user_model)
    called = []
    with transaction.atomic(), pytest.raises(ContractError, match=r"allocation\.observation_in_transaction"):
        record_model_observations(catalog, lambda _: called.append(True) or observations)
    assert not called


def test_optional_absence_is_persisted_and_retry_does_not_later_allocate(
    django_user_model, live_session_credential, allow_all_policy
):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import ModelQuotaReading

    request = prepared_launch(django_user_model, required=False)
    reading = ModelQuotaReading.objects.get()
    saved = reading.observation
    reading.delete()
    command = ["raes-range", "provision", "--request-id", str(request.request_id)]
    credential = launch_credential(request, live_session_credential)
    first = enqueue_provisioner_launch(command, credential=credential)
    assert ProvisionerLaunchIntent.objects.count() == 1
    assert not ModelAllocation.objects.exists()
    ModelQuotaReading.objects.create(
        catalog_digest=saved["catalog_digest"],
        quota_pool_id=saved["quota_pool_id"],
        observed_at=saved["observed_at"],
        observation=saved,
    )
    assert enqueue_provisioner_launch(command, credential=credential) == first
    assert not ModelAllocation.objects.exists()


@pytest.mark.parametrize("reason", ["authority", "egress", "metric"])
def test_optional_unavailability_does_not_abort_launch(
    django_user_model, reason, live_session_credential, allow_all_policy
):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import (
        ModelLaunchPreparationRecord,
        ModelOptionalAbsence,
        ModelQuotaReading,
        SharingAuthorityFence,
    )
    from shared.model_access import seal_catalog

    request = prepared_launch(django_user_model, required=False)
    if reason == "authority":
        SharingAuthorityFence.objects.update(state="unknown")
    elif reason == "egress":
        Range.objects.filter(uuid=request.range_id).update(egress_mode="none")
    else:
        prepared = ModelLaunchPreparationRecord.objects.get()
        payload = prepared.catalog
        payload["quota_pools"][0]["unit"] = "tokens/day"
        prepared.catalog = seal_catalog(payload).model_dump(mode="json")
        prepared.save(update_fields=["catalog"])
        reading = ModelQuotaReading.objects.get()
        reading.catalog_digest = prepared.catalog["digest"]
        reading.observation["catalog_digest"] = prepared.catalog["digest"]
        reading.save(update_fields=["catalog_digest", "observation"])
    enqueue_provisioner_launch(
        ["raes-range", "provision", "--request-id", str(request.request_id)],
        credential=launch_credential(request, live_session_credential),
    )
    assert ProvisionerLaunchIntent.objects.count() == 1
    assert ModelOptionalAbsence.objects.count() == 1
    assert (
        ModelOptionalAbsence.objects.get().reason
        == {
            "authority": "allocation.authority_unavailable",
            "egress": "allocation.egress_incompatible",
            "metric": "allocation.unsupported_metric",
        }[reason]
    )
    assert not ModelAllocation.objects.exists()


@pytest.mark.parametrize("failure", ["owner", "corrupt_intent"])
def test_optional_need_does_not_hide_authority_or_contract_conflicts(
    django_user_model, failure, live_session_credential, allow_all_policy
):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import ModelLaunchPreparationRecord, ModelOptionalAbsence
    from shared.model_access import ContractError

    request = prepared_launch(django_user_model, required=False)
    if failure == "owner":
        from cms.models import RangeInstance
        from cms.models import Request as CmsRequest

        other = django_user_model.objects.create_user(username="different-model-owner")
        principal_uuid = other.identity_principal.uuid
        runtime = Range.objects.get(uuid=request.range_id)
        Range.objects.filter(pk=runtime.pk).update(owner_principal_uuid=principal_uuid)
        EngineRequest.objects.filter(pk=runtime.request_id).update(owner_principal_uuid=principal_uuid)
        projection = RangeInstance.objects.get(uuid=runtime.cms_range_uuid)
        RangeInstance.objects.filter(pk=projection.pk).update(owner_principal_uuid=principal_uuid)
        CmsRequest.objects.filter(pk=projection.request_id).update(owner_principal_uuid=principal_uuid)
        expected = "allocation.owner_mismatch"
    else:
        record = ModelLaunchPreparationRecord.objects.get()
        record.intent = {**record.intent, "needs": []}
        record.save(update_fields=["intent"])
        expected = "allocation.invalid_input"
    with pytest.raises(ContractError, match=expected.replace(".", r"\.")):
        enqueue_provisioner_launch(
            ["raes-range", "provision", "--request-id", str(request.request_id)],
            credential=launch_credential(request, live_session_credential),
        )
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not ModelOptionalAbsence.objects.exists()


def test_renewal_updates_catalog_even_when_intent_is_identical(django_user_model):
    from engine.models import ModelLaunchPreparationRecord
    from engine.services import prepare_model_launch
    from shared.model_access import seal_catalog
    from shared.model_access.reservation import ModelLaunchPreparation

    request = prepared_launch(django_user_model)
    record = ModelLaunchPreparationRecord.objects.get()
    intent = ModelLaunchPreparation.model_validate(record.intent)
    payload = record.catalog
    payload["shards"][0]["weight"] += 1
    catalog = seal_catalog(payload)
    prepare_model_launch(
        request_id=request.request_id,
        owner_ref=intent.owner_ref,
        needs=intent.needs,
        scope=intent.scope,
        authority_revisions=intent.authority_revisions,
        catalog=catalog,
        replace_revoked=True,
    )
    record.refresh_from_db()
    assert record.catalog["digest"] == catalog.digest
