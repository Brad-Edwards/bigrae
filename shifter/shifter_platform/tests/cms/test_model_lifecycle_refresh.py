"""Lifecycle routing must follow the durable package snapshot and native executor."""

import pytest

from cms.exceptions import CMSError
from cms.services._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch
from engine.models import (
    ModelAllocation,
    ModelLaunchPreparationRecord,
    ProvisionerLaunchIntent,
    Range,
)
from engine.services import record_model_observations

from ..engine.services.test_model_allocation import allocation_inputs
from ..engine.test_operation_input_raes import _plan
from .test_model_allocation_dispatch import _launch_credential, setup_launch

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _policy_provider(allow_all_policy):
    """Exercise resume and replay through the mandatory policy boundary."""


def paused_launch(django_user_model, settings, *, native=False):
    owner, request, instance = setup_launch(django_user_model, settings)
    _, _, observations = allocation_inputs(django_user_model)
    record_model_observations(settings.MODEL_ACCESS_CATALOG, lambda _: observations)
    Range.objects.filter(uuid=request.range_id).update(
        status="paused", range_config=_plan() if native else {}, range_backend="gce"
    )
    instance.status = "paused"
    instance.save(update_fields=["status"])
    settings.CLOUD_PROVIDER = "aws"
    settings.LOCAL_PROVISIONER = None
    settings.ENGINE_TASK_CLUSTER = "test-cluster"
    settings.ENGINE_TASK_DEFINITION = "test-taskdef"
    settings.ENGINE_TASK_NETWORK_SECURITY_GROUP_ID = "sg-test"
    settings.ENGINE_TASK_NETWORK_SUBNET_IDS = "subnet-test"
    return owner, request, instance


@pytest.mark.parametrize("policy", ["disabled", "missing"])
def test_optional_policy_removal_cannot_reuse_old_catalog(django_user_model, settings, policy):
    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import ModelOptionalAbsence
    from engine.services import record_model_observations

    _, request, _ = setup_launch(django_user_model, settings, required=False)
    catalog = settings.MODEL_ACCESS_CATALOG
    _, _, observations = allocation_inputs(django_user_model)
    record_model_observations(catalog, lambda _: observations)
    settings.MODEL_ACCESS_ENABLED = False
    if policy == "missing":
        settings.MODEL_ACCESS_CATALOG = None
    prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    command = ["raes-range", "provision", "--request-id", str(request.request_id)]
    credential = _launch_credential(request.request_id)
    first = enqueue_provisioner_launch(command, credential=credential)
    assert not ModelAllocation.objects.exists()
    assert ModelOptionalAbsence.objects.get().reason == "allocation.policy_unavailable"
    assert needs_model_preparation(request.request_id)
    record = ModelLaunchPreparationRecord.objects.get()
    assert record.catalog is None

    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = catalog
    prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert enqueue_provisioner_launch(command, credential=credential) == first
    assert not ModelAllocation.objects.exists()


@pytest.mark.parametrize("native", [False, True])
def test_required_broker_resume_remains_paused_without_dispatch(django_user_model, settings, native):
    from cms.services import resume_range_by_request_id

    _, request, instance = paused_launch(django_user_model, settings, native=native)
    with pytest.raises(CMSError):
        resume_range_by_request_id(_launch_credential(request.request_id), str(request.request_id))
    instance.refresh_from_db()
    assert instance.status == "paused"
    assert Range.objects.get(uuid=request.range_id).status == "paused"
    assert not ModelLaunchPreparationRecord.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not ModelAllocation.objects.exists()
