"""Real native creation, cleanup admission, provider inventory and capacity settlement."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


@pytest.fixture
def warm_runtime(settings, allow_all_policy):
    from cms.models import RaesPackageSource
    from cms.services._warm_pool_reconcile import _reserve_warm_intent
    from engine.models import CapacityReservation, Range, WarmRangeGeneration
    from engine.services import (
        RangeBindings,
        WarmGenerationDraft,
        admit_warm_generation_capacity,
        create_raes_range,
        create_warm_generation,
    )
    from shared.identity_scope import ResourceScope
    from tests.cms._warm_pool_fixtures import service_credential

    credential = service_credential(settings)
    settings.CLOUD_PROVIDER = "gcp"
    settings.ENGINE_TASK_CLUSTER = "synthetic-jobs"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    settings.ENGINE_TASK_SERVICE_ACCOUNT_NAME = "synthetic-worker"
    settings.RAES_PACKAGE_BUCKET = "synthetic-warm-content"
    source = RaesPackageSource.objects.create(
        scenario_id="synthetic-warm-cleanup",
        source_kind="object",
        contract_kind="raes",
        contract_profile="shifter",
        package_ref="synthetic/warm.tar.gz",
        package_version="1",
        package_digest="sha256:" + "a" * 64,
        lock_digest="sha256:" + "c" * 64,
        conformance_status="passed",
        scope_kind="installation",
        owner_principal_uuid=credential.principal.uuid,
        is_operator_published=True,
    )
    request_id, draw_key, scope_ref = uuid4(), uuid4(), uuid4()
    _reserve_warm_intent(credential, source, request_id)
    generation = create_warm_generation(
        WarmGenerationDraft(
            bucket_id="native-warm",
            compatibility_digest="sha256:" + "b" * 64,
            effective_policy_fingerprint="sha256:" + "c" * 64,
            backend="aws",
            range_source="mission_control",
            capacity_partition="synthetic",
            capacity_scope_ref=scope_ref,
            capacity_draw_key=draw_key,
            request_id=request_id,
            idle_deadline=timezone.now() + timezone.timedelta(hours=1),
        )
    )
    CapacityReservation.objects.create(
        event_ref=scope_ref,
        partition_name="synthetic",
        metric_name="vcpu",
        amount=10,
        unit_amount=1,
        consumed=0,
        enforcement="advisory",
        window_start=timezone.now() - timezone.timedelta(hours=1),
        window_end=timezone.now() + timezone.timedelta(hours=1),
    )
    assert not admit_warm_generation_capacity(scope_ref=scope_ref, draw_key=draw_key).blocking
    from cms.models import RangeInstance

    instance = RangeInstance.objects.get(request__request_id=request_id)
    create_raes_range(
        request_id=request_id,
        user_id=None,
        compiled_plan=_compiled_plan(),
        workspace_id=None,
        egress_mode="none",
        bindings=RangeBindings(
            credential=credential,
            resource_scope=ResourceScope("installation"),
            cms_range_uuid=instance.uuid,
            defer_dispatch=True,
        ),
    )
    runtime = Range.objects.get(request__request_id=request_id)
    instance.range_id = runtime.pk
    instance.save(update_fields=["range_id"])
    generation = WarmRangeGeneration.objects.get(pk=generation.pk)
    return SimpleNamespace(
        credential=credential,
        request_id=request_id,
        draw_key=draw_key,
        generation=generation,
        runtime=runtime,
        instance=instance,
    )


def _compiled_plan():
    from shared.raes.dispatch_port import ShifterDispatchResult
    from shared.raes.package_loader import launch_raes_package

    plans = []

    class Port:
        def realize(self, plan, participant_access=()):
            plans.append(plan)
            return ShifterDispatchResult("synthetic", True, "accepted")

    scenario = Path(__file__).parents[2] / "shared/raes/fixtures/launchable/shifter-launch-min.sdl.yaml"
    assert launch_raes_package(scenario_path=scenario, port=Port()).accepted
    return plans[0]


def _ready_payload(intent):
    """Synthetic guest observations for the exact immutable public fixture plan."""
    from engine.models import OperationInput
    from shared.raes.completion_evidence import build_completion_evidence

    immutable = OperationInput.objects.get(operation_id=intent.operation_id).envelope["payload"]
    plan = (immutable["raes_input"] if intent.payload["operation"] == "activate" else immutable)["plan"]
    completion = build_completion_evidence(
        plan,
        generation_id=str(intent.operation_id),
        resources=[
            {"address": address, "resource_type": resource["resource_type"], "status": "provisioned"}
            for address, resource in plan["resources"].items()
        ],
        operating_systems=[
            {
                "instance_key": "provision.node.web#0",
                "family": "linux",
                "distribution": "x-shifter:alpine",
                "version": "3.19",
            }
        ],
        compute_substrates=[{"instance_key": "provision.node.web#0", "value": "virtual-machine"}],
    )
    return {"raes_status": "succeeded", "members": [], "completion": completion}


def _apply_result(intent, step, payload):
    from engine.models import OperationResultDisposition, OperationResultInbox
    from engine.services import apply_pending_operation_results
    from engine.services._launch_authority import launch_authority_digest
    from shared.operation_envelope import build_operation_envelope, canonical_payload_digest
    from shared.operation_results import build_result_identity, result_kind_for

    envelope = build_operation_envelope(
        operation_id=intent.operation_id,
        request_id=intent.payload["request_id"],
        resource="raes-range",
        operation=intent.payload["operation"],
        payload=payload,
        contract_version="2",
        authority_digest=launch_authority_digest(intent),
    )
    digest = canonical_payload_digest(payload)
    result = OperationResultInbox.objects.create(
        operation_id=intent.operation_id,
        request_id=intent.payload["request_id"],
        resource="raes-range",
        operation=intent.payload["operation"],
        contract_version="2",
        result_step=step,
        result_kind=result_kind_for("raes-range", intent.payload["operation"], step=step),
        result_identity=build_result_identity(operation_id=intent.operation_id, step=step, digest=digest),
        payload_digest=digest,
        envelope=envelope,
    )
    apply_pending_operation_results()
    result.refresh_from_db()
    assert result.disposition == OperationResultDisposition.APPLIED, result.disposition_detail


def _cleanup_provider(case):
    """Exercise the actual provisioner cleanup against mutable synthetic SDK state."""
    assert case.runtime.range_backend in {None, "ec2"}
    path = Path(__file__).resolve().parents[4] / "engine/provisioner/ec2_range_cleanup.py"
    spec = importlib.util.spec_from_file_location("synthetic_native_ec2_cleanup", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    scope = module.Ec2CleanupScope(
        "test", "us-east-1", "vpc-" + "0" * 17, case.request_id, case.runtime.resource_generation, case.runtime.pk
    )
    tags = [{"Key": key, "Value": value} for key, value in scope.tags().items()]

    class Provider:
        def __init__(self):
            self.rows = {category: [] for category in module._LOOKUPS}
            self.rows["instances"] = [
                {"InstanceId": "i-" + "0" * 17, "VpcId": scope.vpc_id, "Tags": tags, "State": {"Name": "running"}}
            ]
            self.unavailable = False

        def __getattr__(self, name):
            for category, (operation, key, _) in module._LOOKUPS.items():
                if name == operation:

                    def describe(*, category=category, key=key, **kwargs):
                        if self.unavailable:
                            raise RuntimeError("Synthetic provider unavailable")
                        values = self.rows[category]
                        return {key: [{"Instances": values}] if category == "instances" and values else values}

                    return describe
            raise AttributeError(name)

        def terminate_instances(self, *, InstanceIds):
            assert InstanceIds == [row["InstanceId"] for row in self.rows["instances"]]
            self.rows["instances"].clear()

        def get_waiter(self, name):
            assert name == "instance_terminated"
            return SimpleNamespace(wait=lambda **kwargs: None)

    return module, scope, Provider()


@pytest.mark.parametrize("queued", [False, True])
def test_native_warm_retirement_survives_revocation_and_releases_only_verified_generation(warm_runtime, queued):
    from engine.models import CapacityDraw, ProvisionerLaunchIntent, RangeAuthorityChange
    from engine.services import dispatch_created_raes_range, finalize_retiring_generations, retire_generation
    from engine.services._launch_authority import authorize_launch_authority
    from management.models import ServiceCredentialAdmission
    from shared.operation_results import ResultStep

    case = warm_runtime
    receipt = RangeAuthorityChange.objects.get(range=case.runtime, operation="create")
    assert receipt.authority_kind == "warm_pool" and receipt.resource_generation == case.runtime.resource_generation
    if queued:
        assert dispatch_created_raes_range(case.request_id, credential=case.credential).accepted
        provision = ProvisionerLaunchIntent.objects.get(payload__operation="provision")
        case.runtime.refresh_from_db()
        authorize_launch_authority(provision, case.runtime)
        _apply_result(
            provision,
            ResultStep.RAES_TERMINAL_FAILED,
            {"reason_code": "cloud_operation_failed", "diagnostic": "Synthetic provider failure"},
        )
        case.generation.refresh_from_db()
    ServiceCredentialAdmission.objects.filter(uuid=case.credential.credential_uuid).update(is_active=False)
    assert retire_generation(case.generation)
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    assert cleanup.authority_kind == "cleanup" and cleanup.cleanup_origin_operation_uuid is not None
    case.runtime.refresh_from_db()
    authorize_launch_authority(cleanup, case.runtime)
    assert finalize_retiring_generations(case.generation.bucket_id) == 0
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None
    module, scope, provider = _cleanup_provider(case)
    evidence = module.destroy_ec2_resources(scope, provider)
    assert evidence["outcome"] == "VERIFIED_ABSENT" and not any(provider.rows.values())
    _apply_result(
        cleanup, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": evidence}
    )
    assert finalize_retiring_generations(case.generation.bucket_id) == 1
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is not None
    assert finalize_retiring_generations(case.generation.bucket_id) == 0


def test_unknown_inventory_and_wrong_operation_keep_native_capacity(warm_runtime):
    from engine.models import CapacityDraw, ProvisionerLaunchIntent
    from engine.services import finalize_retiring_generations, record_cleanup_verification, retire_generation
    from shared.operation_results import ResultStep

    case = warm_runtime
    assert retire_generation(case.generation)
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    module, scope, provider = _cleanup_provider(case)
    provider.unavailable = True
    evidence = module.inventory_ec2_resources(scope, provider)
    assert evidence["outcome"] == "INCOMPLETE"
    _apply_result(
        cleanup, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": evidence}
    )
    assert finalize_retiring_generations(case.generation.bucket_id) == 0
    provider.unavailable = False
    absent = module.destroy_ec2_resources(scope, provider)
    for operation in (uuid4(), cleanup.operation_id):
        record_cleanup_verification(
            request_id=case.request_id,
            operation_id=operation,
            outcome=absent["outcome"],
            scope=absent["scope"],
            residual_categories=absent["residual_categories"],
            observed_at=timezone.now(),
        )
        finalized = finalize_retiring_generations(case.generation.bucket_id)
        assert finalized == int(operation == cleanup.operation_id)
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is not None


def test_post_create_failure_retains_draw_and_queues_real_cleanup(warm_runtime):
    from cms.services._warm_pool_reconcile import _abandon_preparation
    from engine.models import CapacityDraw, ProvisionerLaunchIntent

    case = warm_runtime
    _abandon_preparation(case.request_id, case.draw_key)
    assert ProvisionerLaunchIntent.objects.filter(authority_kind="cleanup", payload__operation="destroy").count() == 1
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None


def _prepared_claim(case, django_user_model, live_session_credential):
    from copy import copy

    from django.db import transaction

    from cms.services._warm_pool_claim import _commit_cms_warm_claim
    from engine.models import ProvisionerLaunchIntent
    from engine.services import (
        claim_ready_generation,
        commit_warm_range_claim,
        dispatch_created_raes_range,
        prepare_warm_range_claim,
    )
    from shared.operation_results import ResultStep
    from workspaces.models import Workspace
    from workspaces.services import add_account_member, create_account, hierarchy_target_scope

    dispatch_created_raes_range(case.request_id, credential=case.credential)
    provision = ProvisionerLaunchIntent.objects.get(payload__operation="provision")
    _apply_result(provision, ResultStep.RAES_TERMINAL_READY, _ready_payload(provision))
    case.generation.refresh_from_db()
    stale = copy(case.generation)
    user = django_user_model.objects.create_user(username=f"native-warm-claim-{uuid4()}")
    credential = live_session_credential(user)
    account = create_account(kind="team", name="Native warm claimant")
    add_account_member(account.uuid, credential.principal)
    workspace = Workspace.objects.get(organization__account_id=account.id)
    scope = hierarchy_target_scope("workspace", workspace.uuid)
    prepared = prepare_warm_range_claim(credential, scope)
    claimant = uuid4()
    with transaction.atomic():
        assert (
            claim_ready_generation(
                candidates=[(case.generation.bucket_id, case.generation.compatibility_digest)],
                backend=case.generation.backend,
                range_source=case.generation.range_source,
                claimant_request_id=claimant,
            )
            is not None
        )
        _commit_cms_warm_claim(case.instance, prepared, user.pk)
        commit_warm_range_claim(
            prepared,
            request_id=case.request_id,
            claimant_request_id=claimant,
            cms_range_uuid=case.instance.uuid,
            user_id=user.pk,
        )
    case.generation.refresh_from_db()
    case.runtime.refresh_from_db()
    return prepared, claimant, stale


def test_healthy_claim_and_late_abandon_preserve_customer_custody(
    warm_runtime, django_user_model, live_session_credential
):
    from cms.services._warm_pool_reconcile import _abandon_preparation
    from engine.models import CapacityDraw, ProvisionerLaunchIntent
    from engine.services import retire_generation

    case = warm_runtime
    prepared, _, stale = _prepared_claim(case, django_user_model, live_session_credential)
    assert not retire_generation(stale)
    assert not retire_generation(case.generation)
    _abandon_preparation(case.request_id, case.draw_key)
    case.generation.refresh_from_db()
    case.instance.refresh_from_db()
    assert case.generation.state == "claimed"
    assert case.instance.owner_principal_uuid == prepared.credential.principal.uuid
    assert case.instance.status == "provisioning"
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None


def test_claim_crash_before_activation_uses_current_owner_receipt_and_verified_cleanup(
    warm_runtime, django_user_model, live_session_credential
):
    from engine.models import CapacityDraw, ProvisionerLaunchIntent, WarmRangeGeneration
    from engine.services import finalize_retiring_generations, recover_stalled_generations
    from engine.services._launch_authority import authorize_launch_authority
    from shared.operation_results import ResultStep

    case = warm_runtime
    WarmRangeGeneration.objects.filter(pk=case.generation.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=2)
    )
    prepared, _, _ = _prepared_claim(case, django_user_model, live_session_credential)
    # Time spent quarantined in the pool cannot shorten a fresh claim's grace.
    assert recover_stalled_generations(case.generation.bucket_id, stall_grace_seconds=600) == 0
    WarmRangeGeneration.objects.filter(pk=case.generation.pk).update(
        claimed_at=timezone.now() - timezone.timedelta(hours=2)
    )
    assert recover_stalled_generations(case.generation.bucket_id, stall_grace_seconds=600) == 1
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    case.runtime.refresh_from_db()
    assert cleanup.authority_owner_uuid == prepared.credential.principal.uuid
    authorize_launch_authority(cleanup, case.runtime)
    module, scope, provider = _cleanup_provider(case)
    evidence = module.destroy_ec2_resources(scope, provider)
    _apply_result(
        cleanup, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": evidence}
    )
    assert finalize_retiring_generations(case.generation.bucket_id) == 1
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is not None


def test_completed_activation_cannot_be_retired_from_stale_claim_observation(
    warm_runtime, django_user_model, live_session_credential, monkeypatch
):
    from copy import copy

    from engine.models import ProvisionerLaunchIntent, RangeAuthorityChange, WarmRangeGeneration
    from engine.services import enqueue_admitted_warm_activation, recover_stalled_generations, retire_generation
    from shared.operation_results import ResultStep

    case = warm_runtime
    # Give the genuine accepted claim receipt a UUID below its older creation
    # receipt. UUID sort order must never decide which custody is current.
    monkeypatch.setattr(RangeAuthorityChange._meta.get_field("id"), "_get_default", lambda: UUID(int=1))
    prepared, claimant, _ = _prepared_claim(case, django_user_model, live_session_credential)
    assert (
        RangeAuthorityChange.objects.get(operation="scope").pk < RangeAuthorityChange.objects.get(operation="create").pk
    )
    stale = copy(case.generation)
    enqueue_admitted_warm_activation(prepared, request_id=case.request_id, claimant_request_id=claimant)
    activation = ProvisionerLaunchIntent.objects.get(payload__operation="activate")
    _apply_result(activation, ResultStep.RAES_TERMINAL_READY, _ready_payload(activation))
    assert not retire_generation(stale, _recovery_cutoff=timezone.now())
    WarmRangeGeneration.objects.filter(pk=case.generation.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=2)
    )
    assert recover_stalled_generations(case.generation.bucket_id, stall_grace_seconds=600) == 0
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()


@pytest.mark.parametrize("activation", [False, True])
def test_queued_warm_operation_is_interrupted_through_real_sdk_absence(
    warm_runtime, monkeypatch, django_user_model, live_session_credential, activation
):
    from engine.launch_interrupt import drain_due_interrupts
    from engine.models import ProvisionerLaunchIntent, WarmRangeGeneration
    from engine.services import (
        dispatch_created_raes_range,
        enqueue_admitted_warm_activation,
        finalize_retiring_generations,
        recover_stalled_generations,
        retire_generation,
    )
    from engine.services._launch_authority import authorize_launch_authority
    from shared.cloud.kubernetes import KubernetesTaskRunner

    case = warm_runtime
    if activation:
        prepared, claimant, _ = _prepared_claim(case, django_user_model, live_session_credential)
        enqueue_admitted_warm_activation(prepared, request_id=case.request_id, claimant_request_id=claimant)
        WarmRangeGeneration.objects.filter(pk=case.generation.pk).update(
            claimed_at=timezone.now() - timezone.timedelta(hours=2)
        )
    else:
        dispatch_created_raes_range(case.request_id, credential=case.credential)

    class ApiException(Exception):
        def __init__(self, status):
            self.status = status

    class Cluster:
        jobs = {}
        queries = []

        def read_namespaced_job(self, name, namespace, **kwargs):
            self.queries.append((namespace, name))
            if (namespace, name) not in self.jobs:
                raise ApiException(status=404)
            return self.jobs[namespace, name]

        def list_namespaced_pod(self, **kwargs):
            return SimpleNamespace(items=[])

    cluster = Cluster()
    monkeypatch.setattr(
        KubernetesTaskRunner,
        "_load_kubernetes_api",
        staticmethod(lambda: (cluster, cluster, SimpleNamespace(), ApiException)),
    )
    if activation:
        assert recover_stalled_generations(case.generation.bucket_id, stall_grace_seconds=600) == 1
    else:
        assert retire_generation(case.generation)
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()
    assert drain_due_interrupts(1) == 1
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    case.runtime.refresh_from_db()
    authorize_launch_authority(cleanup, case.runtime)
    assert cluster.queries and not cluster.jobs
    assert finalize_retiring_generations(case.generation.bucket_id) == 0
    assert ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").count() == 1


@pytest.mark.parametrize("changed", ["generation", "owner", "ledger-custody"])
def test_retained_warm_cleanup_refuses_changed_generation_or_custody(warm_runtime, changed):
    from engine.models import CapacityDraw, ProvisionerLaunchIntent, Range, WarmRangeGeneration
    from engine.services import finalize_retiring_generations, retire_generation
    from engine.services._launch_authority import authorize_launch_authority

    case = warm_runtime
    assert retire_generation(case.generation)
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    if changed == "generation":
        Range.objects.filter(pk=case.runtime.pk).update(resource_generation=uuid4())
    elif changed == "owner":
        Range.objects.filter(pk=case.runtime.pk).update(owner_principal_uuid=uuid4())
    else:
        WarmRangeGeneration.objects.filter(pk=case.generation.pk).update(claimed_by_request_id=uuid4())
    case.runtime.refresh_from_db()
    with pytest.raises(PermissionError):
        authorize_launch_authority(cleanup, case.runtime)
    assert finalize_retiring_generations(case.generation.bucket_id) == 0
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None


@pytest.mark.parametrize("disabled", [True, False])
@pytest.mark.parametrize("unknown", [True, False])
def test_worker_retains_removed_policy_cleanup_and_retries_real_inventory(
    warm_runtime, settings, monkeypatch, disabled, unknown
):
    from django.core.management import call_command

    from engine.models import CapacityDraw, OperationInput, ProvisionerLaunchIntent
    from engine.services._launch_authority import authorize_launch_authority
    from shared.operation_results import ResultStep
    from shared.warm_pool.policy import load_policy_json

    case = warm_runtime
    from management.models import ServiceCredentialAdmission

    ServiceCredentialAdmission.objects.filter(uuid=case.credential.credential_uuid).update(is_active=False)
    settings.WARM_POOL_POLICY = load_policy_json(
        ""
        if disabled
        else '{"enabled":true,"buckets":[{"id":"other","backend":"gce","scenario":"other",'
        '"capacity_partition":"synthetic","target":0,"maximum":1,"minimum":0,"idle_ttl_seconds":3600}]}'
    )
    call_command("reconcile_warm_pool")
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    original_input = OperationInput.objects.get(operation_id=cleanup.operation_id).envelope
    generation_id = case.runtime.resource_generation
    module, scope, provider = _cleanup_provider(case)
    provider.unavailable = unknown
    inventory = module.inventory_ec2_resources(scope, provider)
    assert inventory["outcome"] == ("INCOMPLETE" if unknown else "RESIDUALS_FOUND")
    _apply_result(
        cleanup, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": inventory}
    )
    call_command("reconcile_warm_pool")
    assert ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").count() == 1
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None

    moment = timezone.now() + timezone.timedelta(minutes=2)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    call_command("reconcile_warm_pool")
    case.runtime.refresh_from_db()
    retry = ProvisionerLaunchIntent.objects.get(operation_id=case.runtime.provisioner_operation_id)
    assert retry.operation_id != cleanup.operation_id
    assert retry.cleanup_origin_operation_uuid == cleanup.cleanup_origin_operation_uuid
    assert case.runtime.resource_generation == generation_id
    assert OperationInput.objects.get(operation_id=cleanup.operation_id).envelope == original_input
    authorize_launch_authority(retry, case.runtime)
    provider.unavailable = False
    evidence = module.destroy_ec2_resources(scope, provider)
    _apply_result(
        retry, ResultStep.RAES_TERMINAL_DESTROYED, {"raes_status": "succeeded", "cleanup_inventory": evidence}
    )
    call_command("reconcile_warm_pool")
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is not None
    case.generation.refresh_from_db()
    assert case.generation.state == "terminal"


def test_retained_cleanup_cursor_advances_past_an_unknown_generation(warm_runtime):
    from dataclasses import replace

    from engine.models import CapacityDraw, ProvisionerLaunchIntent
    from engine.services import (
        WarmGenerationDraft,
        admit_warm_generation_capacity,
        create_warm_generation,
        reconcile_retained_warm_generations,
        retire_generation,
    )
    from shared.operation_results import ResultStep

    case = warm_runtime
    assert retire_generation(case.generation)
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    module, scope, provider = _cleanup_provider(case)
    provider.unavailable = True
    _apply_result(
        cleanup,
        ResultStep.RAES_TERMINAL_DESTROYED,
        {"raes_status": "succeeded", "cleanup_inventory": module.inventory_ec2_resources(scope, provider)},
    )
    pending = create_warm_generation(
        replace(
            WarmGenerationDraft(
                **{key: getattr(case.generation, key) for key in WarmGenerationDraft.__dataclass_fields__}
            ),
            request_id=uuid4(),
            capacity_draw_key=uuid4(),
        )
    )
    admit_warm_generation_capacity(scope_ref=pending.capacity_scope_ref, draw_key=pending.capacity_draw_key)
    first = reconcile_retained_warm_generations((), now=timezone.now(), stall_grace_seconds=600, batch_size=1)
    assert first.finalized == 0 and first.next_cursor == case.generation.pk
    second = reconcile_retained_warm_generations(
        (),
        now=timezone.now(),
        stall_grace_seconds=600,
        batch_size=1,
        after_pk=first.next_cursor,
    )
    assert second.finalized == 1
    assert CapacityDraw.objects.get(draw_key=case.draw_key).released_at is None
    assert CapacityDraw.objects.get(draw_key=pending.capacity_draw_key).released_at is not None


def test_warm_refill_policy_denial_precedes_slot_and_draw(warm_runtime, settings, monkeypatch, allow_all_policy):
    from dataclasses import replace

    from cms.models import Request
    from cms.services._warm_pool_reconcile import _bucket_compatibility_digest, _warm_prepare_dispatch
    from engine.models import CapacityDraw, WarmRangeGeneration
    from shared.authorization import AuthorizationDecision, DecisionKind
    from shared.range_instantiation_policy import InstantiationPurpose, evaluate_range_backend_admission
    from tests.engine.services.test_warm_reservation import reservation_inputs

    case = warm_runtime
    reservation_inputs(settings, global_limit=10)
    bucket = replace(settings.WARM_POOL_POLICY.buckets[0], scenario=case.instance.scenario_id)
    settings.WARM_POOL_POLICY = replace(
        settings.WARM_POOL_POLICY, buckets=(bucket, *settings.WARM_POOL_POLICY.buckets[1:])
    )
    settings.WARM_POOL_SERVICE_CREDENTIAL_UUID = case.credential.credential_uuid
    calls = []

    def deny(request):
        calls.append(request.action)
        return AuthorizationDecision(DecisionKind.DENIED, "policy_denied")

    monkeypatch.setattr(allow_all_policy, "check", deny)
    before = (Request.objects.count(), WarmRangeGeneration.objects.count(), CapacityDraw.objects.count())
    from cms.models import RaesPackageSource

    source = RaesPackageSource.objects.get(scenario_id=case.instance.scenario_id)
    assert not _warm_prepare_dispatch(
        bucket,
        settings.WARM_POOL_POLICY,
        _bucket_compatibility_digest(bucket, package_digest=source.package_digest, lock_digest=source.lock_digest),
        evaluate_range_backend_admission("gce", InstantiationPurpose.LIVE_FIRE),
    )
    assert calls
    assert (Request.objects.count(), WarmRangeGeneration.objects.count(), CapacityDraw.objects.count()) == before


@pytest.mark.parametrize("phase", ["before_load", "after_reservation"])
def test_warm_refill_rejects_revision_changed_after_bucket_identity_selection(warm_runtime, settings, phase):
    from dataclasses import replace

    from django.db import connection

    from cms.models import RaesPackageSource, Request
    from cms.services._warm_pool_reconcile import (
        _bucket_compatibility_digest,
        _resolve_bucket_identity,
        _warm_prepare_dispatch,
    )
    from engine.models import CapacityDraw, ProvisionerLaunchIntent, WarmRangeGeneration
    from shared.range_instantiation_policy import InstantiationPurpose, evaluate_range_backend_admission
    from tests.engine.services.test_warm_reservation import reservation_inputs

    case = warm_runtime
    reservation_inputs(settings, global_limit=10)
    bucket = replace(settings.WARM_POOL_POLICY.buckets[0], scenario=case.instance.scenario_id)
    settings.WARM_POOL_POLICY = replace(
        settings.WARM_POOL_POLICY, buckets=(bucket, *settings.WARM_POOL_POLICY.buckets[1:])
    )
    settings.WARM_POOL_SERVICE_CREDENTIAL_UUID = case.credential.credential_uuid
    source = RaesPackageSource.objects.get(scenario_id=bucket.scenario)
    source.lock_digest = "sha256:" + "c" * 64
    source.save(update_fields=["lock_digest"])
    digest_a, lock_a = _resolve_bucket_identity(bucket)
    compatibility_a = _bucket_compatibility_digest(bucket, package_digest=digest_a, lock_digest=lock_a)
    if phase == "before_load":
        RaesPackageSource.objects.filter(pk=source.pk).update(package_digest="sha256:" + "d" * 64)
    before = (
        Request.objects.count(),
        WarmRangeGeneration.objects.count(),
        CapacityDraw.objects.count(),
        ProvisionerLaunchIntent.objects.count(),
    )

    class CreationMustNotStart(BaseException):
        pass

    def stop_unexpected_reservation(execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        if sql.startswith(f'INSERT INTO "{WarmRangeGeneration._meta.db_table}"'):
            # Observe the actual service SQL. Stop before a broken implementation
            # can reach storage/provider work; never supply the reservation lock.
            if phase == "before_load":
                raise CreationMustNotStart
            RaesPackageSource.objects.filter(pk=source.pk).update(package_digest="sha256:" + "d" * 64)
        return result

    with connection.execute_wrapper(stop_unexpected_reservation):
        assert not _warm_prepare_dispatch(
            bucket,
            settings.WARM_POOL_POLICY,
            compatibility_a,
            evaluate_range_backend_admission("gce", InstantiationPurpose.LIVE_FIRE),
        )
    after = (
        Request.objects.count(),
        WarmRangeGeneration.objects.count(),
        CapacityDraw.objects.count(),
        ProvisionerLaunchIntent.objects.count(),
    )
    if phase == "before_load":
        assert after == before
    else:
        # A revision changed after count reservation. The existing source fences
        # prevent CMS/native creation and settle the never-realized draw.
        assert after == (before[0], before[1] + 1, before[2] + 1, before[3])
        rejected = WarmRangeGeneration.objects.exclude(pk=case.generation.pk).get()
        assert rejected.state == "terminal" and rejected.range_id is None
        assert CapacityDraw.objects.get(draw_key=rejected.capacity_draw_key).released_at is not None
