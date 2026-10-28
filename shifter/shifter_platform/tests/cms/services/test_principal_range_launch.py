"""Principal range launches retain native leases, scope and queue admission."""

import pytest

from tests.cms.services.test_principal_content import content_actor
from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["content_actor", "principal_range"]
pytestmark = [pytest.mark.django_db]


def test_service_owned_range_uses_current_tenant_lease_policy(principal_range):
    from cms.models import MissionControlTenantLeasePolicy
    from cms.services._range_lease import build_resolved_mission_control_range_lease

    MissionControlTenantLeasePolicy.objects.create(
        id=1, initial_days=3, extension_days=2, maximum_days=6, extensions_enabled=True, revision=4
    )
    lease = build_resolved_mission_control_range_lease(principal_range.owner)
    assert lease.initial_days == 3
    assert lease.maximum_days == 6
    assert lease.extension_days == 2
    assert lease.policy_source == "runtime"
    assert lease.tenant_revision == 4
    assert lease.group_revisions == ()


def test_human_principal_owner_retains_restrictive_native_group_lease(principal_range, django_user_model):
    from django.contrib.auth.models import Group

    from cms.models import MissionControlGroupLeasePolicy
    from cms.services._range_lease import build_resolved_mission_control_range_lease
    from shared.identity_scope import PrincipalRef

    user = django_user_model.objects.get(username="synthetic-account-owner")
    group = Group.objects.create(name="synthetic-restrictive-lease")
    user.groups.add(group)
    MissionControlGroupLeasePolicy.objects.create(
        group=group, initial_days=1, extension_days=1, maximum_days=2, extensions_enabled=False, revision=3
    )
    owner = PrincipalRef(principal_range.account.individual_principal_uuid, "human")
    lease = build_resolved_mission_control_range_lease(owner)
    assert lease.initial_days == 1
    assert lease.maximum_days == 2
    assert lease.policy_source == "group"
    assert lease.group_revisions == ((group.pk, 3),)


def test_inactive_principal_cannot_receive_a_new_owned_range_lease(principal_range):
    from cms.services._range_lease import build_resolved_mission_control_range_lease
    from management.models import Principal

    Principal.objects.filter(uuid=principal_range.owner.uuid).update(is_active=False)
    with pytest.raises(ValueError):
        build_resolved_mission_control_range_lease(principal_range.owner)


@pytest.fixture
def launch_actor(content_actor, settings, make_pack, tmp_path):
    from uuid import uuid4

    from cms.models import RaesPackageSource
    from cms.scenarios.pack_validation import pack_digest
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from workspaces.services import add_account_member

    actor = content_actor
    admission = ServiceCredentialAdmission.objects.select_related("binding").get(uuid=actor.credential.credential_uuid)
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=admission.audience,
        scopes=["authorization:account.launch_range", "authorization:account.read_content"],
    )
    actor.credential = resolve_service_credential(
        issuer=admission.binding.issuer, subject=admission.binding.subject, audience=admission.audience
    )
    add_account_member(actor.scope.account_uuid, actor.credential.principal)
    root = make_pack(tmp_path / "packs" / "synthetic-launch", name="synthetic-launch")
    settings.RAES_PACKAGE_ROOT = str(tmp_path)
    settings.CLOUD_PROVIDER = "aws"
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    actor.source = RaesPackageSource.objects.create(
        scenario_id="synthetic-launch",
        contract_kind="raes",
        contract_profile="shifter",
        source_kind="repo",
        package_ref="packs/synthetic-launch",
        package_version="1.0.0",
        package_digest=pack_digest(root),
        conformance_status="passed",
        owner_principal_uuid=actor.credential.principal.uuid,
        scope_kind="account",
        account_uuid=actor.scope.account_uuid,
    )
    return actor


def test_service_launch_retains_account_scope_and_original_authority(launch_actor):
    from cms.models import RangeInstance
    from cms.services import create_raes_native_range
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import OperationInput, ProvisionerLaunchIntent, Range
    from management.models import ServiceCredentialAdmission

    actor = launch_actor
    result = create_raes_native_range(actor.credential, actor.source.scenario_id, scope=actor.scope)
    instance = RangeInstance.objects.select_related("request").get(request__request_id=result.request_id)
    runtime = Range.objects.select_related("request").get(request__request_id=result.request_id)
    assert instance.user_id is runtime.user_id is None
    assert instance.owner_principal_uuid == runtime.owner_principal_uuid == actor.credential.principal.uuid
    assert (
        instance.request.owner_principal_uuid == runtime.request.owner_principal_uuid == actor.credential.principal.uuid
    )
    assert instance.scope_kind == runtime.scope_kind == "account"
    assert instance.workspace_id is runtime.workspace_id is None
    assert instance.account_id == runtime.account_id
    assert instance.uuid == runtime.cms_range_uuid
    assert instance.range_id == runtime.pk
    assert result.owner_principal_uuid == actor.credential.principal.uuid
    queued = ProvisionerLaunchIntent.objects.get()
    assert queued.authority_action == "account.launch_range"
    assert queued.authority_account_uuid == actor.scope.account_uuid
    assert queued.initiating_principal_uuid == actor.credential.principal.uuid
    assert queued.credential_uuid == actor.credential.credential_uuid
    assert queued.executing_workload["kind"] == "kubernetes"
    assert queued.executing_workload["namespace"] == "synthetic-namespace"
    assert queued.executing_workload["image"].startswith("example.test/synthetic@sha256:")
    assert queued.executing_workload["task_identity"] == str(queued.intent_id)
    assert str(actor.credential.principal.uuid) not in str(queued.executing_workload)
    assert OperationInput.objects.count() == 1
    authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=actor.credential.credential_uuid).update(is_active=False)
    with pytest.raises(PermissionError):
        authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)


def test_service_launch_denies_sibling_account_before_reservation(launch_actor):
    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.services import create_raes_native_range
    from engine.models import ProvisionerLaunchIntent, Range

    with pytest.raises(CMSError):
        create_raes_native_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.sibling)
    assert not RangeInstance.all_objects.exists()
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.parametrize("change", ["credential", "source"])
def test_launch_revalidates_after_compilation_before_native_creation(launch_actor, monkeypatch, change):
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, RangeInstance
    from cms.services import create_range
    from engine.models import OperationInput, ProvisionerLaunchIntent, Range
    from management.models import ServiceCredentialAdmission
    from shared.raes import package_loader

    launch = package_loader.launch_raes_package

    def change_before_apply(**kwargs):
        if change == "credential":
            ServiceCredentialAdmission.objects.filter(uuid=launch_actor.credential.credential_uuid).update(
                is_active=False
            )
        else:
            RaesPackageSource.objects.filter(pk=launch_actor.source.pk).update(conformance_status="failed")
        return launch(**kwargs)

    monkeypatch.setattr(package_loader, "launch_raes_package", change_before_apply)
    with pytest.raises(CMSError):
        create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    assert RangeInstance.all_objects.get().status == "failed"
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()


@pytest.mark.parametrize("changed", ["publication", "enabled", "staff_only", "conformance", "lock"])
def test_cold_launch_rejects_source_eligibility_changed_after_native_admission(launch_actor, monkeypatch, changed):
    from cms.exceptions import CMSError
    from cms.models import RaesPackageSource, RangeInstance, ScenarioMetadata
    from cms.services import create_range
    from engine import services as engine_services
    from engine.models import OperationInput, ProvisionerLaunchIntent, Range

    actor = launch_actor
    if changed == "publication":
        RaesPackageSource.objects.filter(pk=actor.source.pk).update(
            scope_kind="installation", account_uuid=None, is_operator_published=True
        )
        actor.source.refresh_from_db()
    original = engine_services.prepare_raes_range_creation

    def prepare_then_change(*args, **kwargs):
        prepared = original(*args, **kwargs)
        if changed in {"enabled", "staff_only"}:
            ScenarioMetadata.objects.update_or_create(
                scenario_id=actor.source.scenario_id, defaults={changed: changed != "enabled"}
            )
        else:
            field, value = {
                "publication": ("is_operator_published", False),
                "conformance": ("conformance_status", "failed"),
                "lock": ("lock_digest", "sha256:" + "f" * 64),
            }[changed]
            RaesPackageSource.objects.filter(pk=actor.source.pk).update(**{field: value})
        return prepared

    monkeypatch.setattr(engine_services, "prepare_raes_range_creation", prepare_then_change)
    with pytest.raises(CMSError):
        create_range(actor.credential, actor.source.scenario_id, scope=actor.scope)
    assert RangeInstance.all_objects.get().status == "failed"
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()


def test_service_launch_cannot_occupy_a_second_active_slot(launch_actor):
    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.services import create_range
    from engine.models import ProvisionerLaunchIntent, Range

    create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    with pytest.raises(CMSError):
        create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    assert RangeInstance.objects.count() == Range.objects.count() == ProvisionerLaunchIntent.objects.count() == 1


def test_unauthorized_dispatch_does_not_change_a_principal_range(launch_actor):
    from cms.services import create_range
    from engine.models import Range
    from engine.services import dispatch_created_raes_range

    result = create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    runtime = Range.objects.get(request__request_id=result.request_id)
    with pytest.raises(PermissionError):
        dispatch_created_raes_range(result.request_id)
    runtime.refresh_from_db()
    assert runtime.status == "provisioning"


def test_principal_provision_has_transactional_audit(launch_actor):
    from cms.services import create_range
    from shared.models import AuditLog

    result = create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    events = AuditLog.objects.filter(request_id=str(result.request_id), action="provision")
    assert events.exists()
    assert set(events.values_list("actor_principal_uuid", flat=True)) == {launch_actor.credential.principal.uuid}


def test_failed_audit_rolls_back_native_range_and_queue(launch_actor, monkeypatch):
    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.services import create_range
    from engine.models import OperationInput, ProvisionerLaunchIntent, Range

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("shared.audit.audit_log", unavailable)
    with pytest.raises(CMSError):
        create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    assert RangeInstance.all_objects.get().status == "failed"
    assert not Range.objects.exists()
    assert not OperationInput.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.fixture
def failed_launch(launch_actor, monkeypatch):
    from uuid import uuid4

    from cms.exceptions import CMSError
    from cms.models import RangeInstance
    from cms.raes.dispatch import CmsRaesDispatchPort
    from cms.services import create_range
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential

    admission = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=launch_actor.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=admission.audience,
        scopes=[
            *admission.scopes,
            "authorization:range.read",
            "authorization:range.manage",
            "authorization:range.access",
        ],
    )
    launch_actor.credential = resolve_service_credential(
        issuer=admission.binding.issuer, subject=admission.binding.subject, audience=admission.audience
    )

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic delivery preparation failure")

    with monkeypatch.context() as fault:
        fault.setattr(CmsRaesDispatchPort, "_prepare_delivery", unavailable)
        with pytest.raises(CMSError):
            create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    launch_actor.failed = RangeInstance.all_objects.select_related("request").get()
    assert launch_actor.failed.status == "failed" and launch_actor.failed.range_id is None
    return launch_actor


def test_unrealized_failure_remains_readable_through_history_and_detail(failed_launch):
    from cms.exceptions import CMSError
    from cms.services import get_range_by_request_id, list_mission_control_range_history, require_range_action
    from engine.models import Range
    from shared.authorization import TargetRef
    from shared.credentials import resolve_credential_scope

    actor = failed_launch
    assert not Range.objects.exists()
    history = list_mission_control_range_history(actor.credential, scope=actor.scope)
    assert [row.uuid for row in history] == [actor.failed.uuid]
    detail = get_range_by_request_id(actor.credential, str(actor.failed.request.request_id), include_terminal=True)
    assert detail.status.value == "failed" and detail.range_id is None and detail.instances == []
    assert not detail.pause_supported and not detail.resume_supported
    assert resolve_credential_scope(TargetRef("range", actor.failed.uuid)).account_uuid == actor.scope.account_uuid
    for action in ("range.manage", "range.access"):
        with pytest.raises(CMSError):
            require_range_action(actor.credential, actor.failed.uuid, action, include_terminal=True)


@pytest.mark.parametrize("change", ["policy-denied", "policy-outage", "request-custody", "event-parent", "runtime"])
def test_unrealized_failure_read_still_requires_current_policy_and_consistent_custody(
    failed_launch, monkeypatch, change
):
    from types import SimpleNamespace
    from uuid import uuid4

    from cms.exceptions import CMSError
    from cms.models import RangeInstance, Request
    from cms.services import get_range_by_request_id, list_mission_control_range_history
    from engine.models import Range
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    actor = failed_launch
    if change.startswith("policy-"):
        kind = DecisionKind.DENIED if change == "policy-denied" else DecisionKind.EVALUATOR_ERROR
        reason = "policy_denied" if change == "policy-denied" else "evaluator_unavailable"
        monkeypatch.setattr(
            port,
            "_provider_factory",
            lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(kind, reason)),
        )
    elif change == "request-custody":
        from workspaces.services import resolve_resource_scope

        Request.objects.filter(pk=actor.failed.request_id).update(
            account_id=resolve_resource_scope(actor.sibling).account_id
        )
    elif change == "event-parent":
        RangeInstance.all_objects.filter(pk=actor.failed.pk).update(event_uuid=uuid4())
    else:
        # An existing mismatched runtime cannot be treated as an unrealized intent.
        Range.objects.create(cms_range_uuid=actor.failed.uuid, status="failed", workspace_id=1)
    with pytest.raises(CMSError):
        get_range_by_request_id(actor.credential, str(actor.failed.request.request_id), include_terminal=True)
    if change == "policy-outage":
        with pytest.raises(CMSError):
            list_mission_control_range_history(actor.credential, scope=actor.scope)
    else:
        assert list_mission_control_range_history(actor.credential, scope=actor.scope) == []


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_launch_policy_is_evaluated_outside_native_sql_transactions(launch_actor, monkeypatch):
    from django.db import connection

    from cms.services import create_range
    from shared.authorization import port

    provider = port._provider_factory()

    def check(request):
        assert not connection.in_atomic_block, "Policy I/O cannot run while the creation transaction holds row locks"
        return provider.check(request)

    from types import SimpleNamespace

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)


def test_launch_diagnostic_does_not_log_private_provider_material(launch_actor, monkeypatch, caplog):
    from cms.exceptions import CMSError
    from cms.raes.dispatch import CmsRaesDispatchPort
    from cms.services import create_range

    def failed(*args, **kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_PROVIDER_MARKER")

    monkeypatch.setattr(CmsRaesDispatchPort, "_prepare_delivery", failed)
    with pytest.raises(CMSError) as caught:
        create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    assert "SYNTHETIC_PRIVATE_PROVIDER_MARKER" not in str(caught.value)
    assert "SYNTHETIC_PRIVATE_PROVIDER_MARKER" not in caplog.text


def test_gce_principal_launch_pins_a_generation_for_remote_and_cleanup_authority(launch_actor, settings, monkeypatch):
    from cms.services import create_range
    from engine.models import Range

    settings.CLOUD_PROVIDER = "gcp"
    monkeypatch.setenv("GCP_RANGE_BACKEND", "gce")
    monkeypatch.delenv("GCP_RANGE_PLANE", raising=False)
    result = create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    runtime = Range.objects.get(request__request_id=result.request_id)
    assert runtime.resource_generation is not None


def test_queued_launch_cannot_change_executing_identity_before_dispatch(launch_actor, settings, monkeypatch):
    from types import SimpleNamespace

    from cms.services import create_range
    from engine.ecs import dispatch_provisioner_command
    from engine.launch_intents import command_from_payload
    from engine.models import ProvisionerLaunchIntent

    create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    queued = ProvisionerLaunchIntent.objects.get()
    calls = []
    monkeypatch.setattr(
        "engine.ecs.get_task_runner", lambda: SimpleNamespace(run_task=lambda **kwargs: calls.append(kwargs))
    )
    settings.ENGINE_TASK_DEFINITION = "example.test/replaced@sha256:" + "2" * 64
    command = command_from_payload({**queued.payload, "operation_id": str(queued.operation_id)})
    with pytest.raises(ValueError, match="workload changed"):
        dispatch_provisioner_command(
            command, task_identity=str(queued.intent_id), expected_workload=queued.executing_workload
        )
    assert not calls


@pytest.mark.postgres
def test_bulk_write_cannot_substitute_a_queued_executing_workload(launch_actor):
    from django.db import IntegrityError, transaction

    from cms.services import create_range
    from engine.models import ProvisionerLaunchIntent

    create_range(launch_actor.credential, launch_actor.source.scenario_id, scope=launch_actor.scope)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProvisionerLaunchIntent.objects.update(executing_workload={"namespace": "foreign"})


@pytest.mark.parametrize(
    ("has_owner", "has_action", "has_target"),
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (False, True, True),
        (True, True, False),
        (True, False, True),
        (True, True, True),
    ],
)
def test_mission_control_rejects_event_binding_fields_before_any_reservation(
    launch_actor, has_owner, has_action, has_target
):
    from cms.exceptions import CMSError
    from cms.models import RangeInstance, Request
    from cms.services import create_raes_native_range
    from cms.services._scoped_launch_types import NativeEventLaunchOptions
    from engine.models import ProvisionerLaunchIntent, Range
    from management.services import create_service_principal
    from shared.authorization import TargetRef

    actor = launch_actor
    unrelated_owner = create_service_principal("Synthetic unrelated range owner")
    binding = NativeEventLaunchOptions(
        owner_principal=unrelated_owner if has_owner else None,
        authority_action="account.launch_range" if has_action else None,
        authority_target=TargetRef("account", actor.scope.account_uuid) if has_target else None,
    )
    with pytest.raises(CMSError):
        create_raes_native_range(actor.credential, actor.source.scenario_id, scope=actor.scope, event_options=binding)
    assert not Request.objects.exists()
    assert not RangeInstance.all_objects.exists()
    assert not Range.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
