"""CMS supplies actual scenario/owner demand before the Engine launch commits."""

import pytest

from cms.models import RangeInstance, Request
from engine.models import ModelLaunchPreparationRecord
from shared.model_access import ContractError

from ..engine.services.test_model_allocation import allocation_inputs
from .test_launch_model_admission import _author, _need_payload, _register

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _policy_provider(personal_vpn_policy_grant):
    """Exercise allocation through the mandatory application-policy boundary."""


def setup_launch(django_user_model, settings, *, owner=None, required=True):
    from django.test import Client

    from config.session_credentials import session_credential
    from engine.models import Range
    from shared.operation_authority import credential_reference_fields

    catalog, request, _ = allocation_inputs(django_user_model, user=owner)
    engine_range = Range.objects.get(uuid=request.range_id)
    actor = engine_range.user
    if actor.is_active:
        authority_user = actor
    else:
        authority_user = django_user_model.objects.create_user(username=f"synthetic-model-preparer-{actor.pk}")
    client = Client()
    client.force_login(authority_user, backend="config.auth.PlatformModelBackend")
    credential = session_credential(authority_user, client.session)
    engine_range.owner_principal_uuid = credential.principal.uuid
    engine_range.save(update_fields=["owner_principal_uuid"])
    engine_range.request.owner_principal_uuid = credential.principal.uuid
    engine_range.request.save(update_fields=["owner_principal_uuid"])
    account_id = engine_range.account_id
    _register(actor)
    _author(actor, needs={"participant": _need_payload(digest=request.need.scenario_digest, required=required)})
    cms_request = Request.objects.create(
        request_id=request.request_id,
        request_type="range",
        user=actor,
        scope_kind="account",
        account_id=account_id,
        owner_principal_uuid=credential.principal.uuid,
        **credential_reference_fields(credential),
    )
    instance = RangeInstance.objects.create(
        request=cms_request,
        scenario_id="scn",
        user_id=actor.pk,
        owner_principal_uuid=credential.principal.uuid,
        scope_kind="account",
        account_id=account_id,
        range_source="mission_control",
        model_package_digest=request.need.scenario_digest,
        expires_at=request.window_end,
        maximum_expires_at=request.window_end,
    )
    engine_range.cms_range_uuid = instance.uuid
    engine_range.save(update_fields=["cms_range_uuid"])
    instance.range_id = engine_range.pk
    instance.save(update_fields=["range_id"])
    settings.MODEL_ACCESS_ENABLED = True
    settings.MODEL_ACCESS_CATALOG = catalog
    settings.CLOUD_PROVIDER = "aws"
    settings.ENGINE_TASK_CLUSTER = "test-cluster"
    settings.ENGINE_TASK_DEFINITION = "test-taskdef"
    settings.ENGINE_TASK_NETWORK_SECURITY_GROUP_ID = "sg-test"
    settings.ENGINE_TASK_NETWORK_SUBNET_IDS = "subnet-test"
    return actor, request, instance


def setup_warm_launch(django_user_model, settings, credential, *, required=True):
    """Convert a normal model launch fixture into service-owned installation custody."""
    from engine.models import Range
    from shared.operation_authority import credential_reference_fields

    _, request, instance = setup_launch(django_user_model, settings, required=required)
    from cms.models import RaesPackageSource

    RaesPackageSource.objects.filter(scenario_id=instance.scenario_id).update(
        owner_principal_uuid=credential.principal.uuid,
        scope_kind="installation",
        account_uuid=None,
        organization_uuid=None,
        workspace_uuid=None,
        is_operator_published=True,
    )
    engine_range = Range.objects.select_related("request").get(uuid=request.range_id)
    engine_range.user = None
    engine_range.cms_user_id = None
    engine_range.owner_principal_uuid = credential.principal.uuid
    engine_range.scope_kind = "installation"
    engine_range.account_id = None
    engine_range.organization_id = None
    engine_range.workspace_id = None
    engine_range.save(
        update_fields=[
            "user",
            "cms_user_id",
            "owner_principal_uuid",
            "scope_kind",
            "account_id",
            "organization_id",
            "workspace_id",
        ]
    )
    engine_request = engine_range.request
    engine_request.user = None
    engine_request.owner_principal_uuid = credential.principal.uuid
    engine_request.scope_kind = "installation"
    engine_request.account_id = None
    engine_request.organization_id = None
    engine_request.workspace_id = None
    engine_request.save(
        update_fields=[
            "user",
            "owner_principal_uuid",
            "scope_kind",
            "account_id",
            "organization_id",
            "workspace_id",
        ]
    )
    cms_request = instance.request
    cms_request.user = None
    cms_request.owner_principal_uuid = credential.principal.uuid
    cms_request.scope_kind = "installation"
    cms_request.account_id = None
    cms_request.organization_id = None
    cms_request.workspace_id = None
    authority = credential_reference_fields(credential)
    for name, value in authority.items():
        setattr(cms_request, name, value)
    cms_request.save(
        update_fields=[
            "user",
            "owner_principal_uuid",
            "scope_kind",
            "account_id",
            "organization_id",
            "workspace_id",
            *authority,
        ]
    )
    instance.user_id = None
    instance.owner_principal_uuid = credential.principal.uuid
    instance.scope_kind = "installation"
    instance.account_id = None
    instance.organization_id = None
    instance.workspace_id = None
    instance.expires_at = None
    instance.maximum_expires_at = None
    instance.save(
        update_fields=[
            "user_id",
            "owner_principal_uuid",
            "scope_kind",
            "account_id",
            "organization_id",
            "workspace_id",
            "expires_at",
            "maximum_expires_at",
        ]
    )
    return credential, request, instance


def _launch_credential(request_id):
    from cms.models import Request as CMSRequest
    from shared.operation_authority import credential_reference_from_record

    return credential_reference_from_record(CMSRequest.objects.get(request_id=request_id))


def test_replaced_package_cannot_authorize_the_launched_snapshot(django_user_model, settings):
    from cms.services._model_allocation import prepare_model_access_for_dispatch

    _, request, instance = setup_launch(django_user_model, settings)
    instance.model_package_digest = "sha256:" + "f" * 64
    instance.save(update_fields=["model_package_digest"])
    with pytest.raises(ContractError, match=r"allocation\.scenario_unavailable"):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()


def test_sibling_ready_transition_does_not_revoke_shared_owner_grant(django_user_model):
    from engine.models import ModelPendingGrant, Range

    from ..engine.services.test_model_allocation import allocate

    owner = django_user_model.objects.create_user(username="model-sibling-owner")
    first = allocation_inputs(django_user_model, user=owner)
    first_allocation = allocate(first)
    second = allocation_inputs(django_user_model, user=owner)
    second_allocation = allocate(second)
    range_obj = Range.objects.get(uuid=first[1].range_id)
    range_obj.status = Range.Status.READY
    range_obj.save(update_fields=["status"])
    assert ModelPendingGrant.objects.get(allocation=first_allocation).state == "pending"
    assert ModelPendingGrant.objects.get(allocation=second_allocation).state == "pending"


def test_disabled_owner_cannot_publish_allowed_projection(django_user_model, settings):
    from cms.services._model_allocation import prepare_model_access_for_dispatch

    actor, request, _ = setup_launch(django_user_model, settings)
    actor.is_active = False
    actor.save(update_fields=["is_active"])
    with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()


def test_required_broker_preparation_remains_unavailable(django_user_model, settings):
    from cms.services._model_allocation import prepare_model_access_for_dispatch
    from engine.models import ModelAllocation, ProvisionerLaunchIntent

    _, request, _ = setup_launch(django_user_model, settings)
    with pytest.raises(ContractError, match=r"allocation\.authority_unavailable"):
        prepare_model_access_for_dispatch(request.request_id, range_id=request.range_id)
    assert not ModelLaunchPreparationRecord.objects.exists()
    assert not ModelAllocation.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


def test_retained_range_owner_change_revokes_its_launch_grant(django_user_model):
    from engine.models import Range
    from engine.services import project_model_launch_authority
    from shared.model_access import OwnedReference
    from tests.engine.services.test_model_allocation import allocate

    catalog, request, observations = allocation_inputs(django_user_model)
    revisions = project_model_launch_authority(
        deployment_id=catalog.deployment_id,
        authority_refs=(OwnedReference(owner="engine", reference=f"model-launch:{request.range_id}"),),
    )
    request = request.model_copy(update={"authority_revisions": request.authority_revisions + revisions})
    allocation = allocate((catalog, request, observations))
    runtime = Range.objects.get(uuid=request.range_id)
    replacement = django_user_model.objects.create_user(username="replacement-range-owner")
    runtime.owner_principal_uuid = replacement.identity_principal.uuid
    runtime.save(update_fields=["owner_principal_uuid"])
    allocation.grant.refresh_from_db()
    assert allocation.grant.state == "revoked"


def test_retained_allocation_deadline_uses_the_session_window(django_user_model):
    from tests.engine.services.test_model_allocation import allocate

    inputs = allocation_inputs(django_user_model)
    allocation = allocate(inputs)
    assert allocation.deadline == inputs[1].window_end
