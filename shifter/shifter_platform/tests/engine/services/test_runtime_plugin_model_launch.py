"""Model admission and isolated adapter planning share one atomic launch."""

import pytest
from django.db import transaction

from engine.launch_intents import enqueue_provisioner_launch
from engine.models import (
    ModelAllocation,
    ModelPendingGrant,
    ModelQuotaReading,
    OperationInput,
    ProvisionerLaunchIntent,
    Range,
    RuntimePluginInstallation,
    RuntimePluginInvocation,
)
from engine.services import bind_runtime_plugin, install_runtime_plugin
from engine.services._runtime_plugin_bindings import persist_runtime_plugin_pin, resolve_runtime_plugin_pin
from shared.model_access import ContractError
from shared.runtime_plugin_binding import RuntimePluginScope
from workspaces.models import Organization, OrganizationMembership

from .test_model_allocation_launch import launch_credential, prepared_launch

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("provisioner_workload_config"),
]


def _prepared_plugin_launch(django_user_model, live_session_credential):
    request = prepared_launch(django_user_model)
    target = Range.objects.get(uuid=request.range_id)
    organization = Organization.objects.create(name="Combined launch")
    OrganizationMembership.objects.create(user=target.user, organization=organization, role="admin")
    installed = install_runtime_plugin(
        target.user,
        organization.uuid,
        {
            "protocol": "shifter.runtime-plugin/v1",
            "plugin_id": "example.adapter",
            "version": "1",
            "distribution": "example-adapter",
            "entry_point": "example",
            "worker_image": "registry.example.test/adapter@sha256:" + "a" * 64,
            "capabilities": ["guest.configure", "guest.verify"],
            "required_bindings": ["server"],
            "model_bindings": {"participant": "server"},
        },
    )
    RuntimePluginInstallation.objects.filter(pk=installed.id).update(state="ready")
    scope = RuntimePluginScope(
        organization_uuid=organization.uuid,
        pack_id="example",
        pack_digest=request.need.scenario_digest,
    )
    bind_runtime_plugin(
        target.user,
        RuntimePluginScope(organization_uuid=organization.uuid, pack_digest=scope.pack_digest, pack_id=scope.pack_id),
        installed.id,
        {"targets": {"server": "node.web"}},
    )
    with transaction.atomic():
        pin = resolve_runtime_plugin_pin(scope, target.range_config)
        persist_runtime_plugin_pin(target, pin)
    return request, target, launch_credential(request, live_session_credential)


def test_model_grant_and_plugin_invocations_use_the_same_committed_generation(
    django_user_model, live_session_credential, allow_all_policy
):
    request, target, credential = _prepared_plugin_launch(django_user_model, live_session_credential)
    command = ["raes-range", "provision", "--request-id", str(request.request_id)]
    first = enqueue_provisioner_launch(command, credential=credential)
    assert enqueue_provisioner_launch(command, credential=credential) == first
    intent = ProvisionerLaunchIntent.objects.get()
    allocation = ModelAllocation.objects.get()
    assert allocation.operation_id == intent.operation_id
    assert allocation.grant.state == "pending"
    projected = OperationInput.objects.get(operation_id=intent.operation_id).envelope["payload"]
    assert projected["model_enrollments"] == [
        {
            "allocation_id": str(allocation.pk),
            "workload_role": "participant",
            "target_address": "node.web",
        }
    ]
    assert "token" not in str(projected["model_enrollments"])
    invocations = RuntimePluginInvocation.objects.all()
    assert set(invocations.values_list("phase", flat=True)) == {"validate", "configure", "verify"}
    assert set(invocations.values_list("operation_id", flat=True)) == {intent.operation_id}
    assert all(row.input["range_id"] == target.pk for row in invocations)


@pytest.mark.parametrize("failure", ["quota", "plugin-input"])
def test_rejected_launch_rolls_back_grant_input_intent_and_plugin_work(
    django_user_model, failure, live_session_credential, allow_all_policy
):
    request, target, credential = _prepared_plugin_launch(django_user_model, live_session_credential)
    if failure == "quota":
        ModelQuotaReading.objects.all().delete()
        error = ContractError
    else:
        # The retained pin still names a node, but isolated worker input cannot
        # represent this OS. Rejection occurs after allocation in the same txn.
        target.range_config["resources"]["node.web"]["payload"]["os_family"] = "unsupported"
        target.save(update_fields=["range_config"])
        from shared.raes.operation_input import RaesOperationInputError

        error = RaesOperationInputError
    with pytest.raises(error):
        enqueue_provisioner_launch(
            ["raes-range", "provision", "--request-id", str(request.request_id)], credential=credential
        )
    assert not ModelAllocation.objects.exists()
    assert not ModelPendingGrant.objects.exists()
    assert not OperationInput.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not RuntimePluginInvocation.objects.exists()
    target.refresh_from_db()
    assert target.provisioner_operation_id is None


@pytest.mark.parametrize("fault", ["target", "role", "duplicate", "allocation"])
def test_guest_enrollment_projection_rejects_foreign_or_ambiguous_bindings(
    django_user_model, fault, live_session_credential, allow_all_policy
):
    from shared.raes.operation_input import RaesOperationInputError, parse_raes_operation_input

    request, _, credential = _prepared_plugin_launch(django_user_model, live_session_credential)
    enqueue_provisioner_launch(
        ["raes-range", "provision", "--request-id", str(request.request_id)], credential=credential
    )
    payload = OperationInput.objects.get().envelope["payload"]
    row = payload["model_enrollments"][0]
    if fault == "duplicate":
        payload["model_enrollments"].append(dict(row))
    else:
        field = {"target": "target_address", "role": "workload_role", "allocation": "allocation_id"}[fault]
        row[field] = "foreign"
    with pytest.raises(RaesOperationInputError):
        parse_raes_operation_input(payload)
