"""Independent principals use the incumbent preparation/attempt lifecycle."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.authorization import AuthorizationDecision, DecisionKind
from shared.identity_scope import ResourceScope
from tests.engine.services.test_preparation_controller import deployment_scope, runtime
from tests.engine.services.test_preparation_operations import administrator, grant, installed, package_input

pytestmark = [pytest.mark.django_db, pytest.mark.postgres]
__all__ = ["administrator", "deployment_scope", "grant", "installed", "runtime"]


@pytest.fixture
def service_preparer(settings, monkeypatch):
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, resolve_service_credential
    from shared.authorization import port

    principal = create_service_principal("Synthetic artifact preparer")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid), issuer="https://accounts.google.com", subject=str(uuid4())
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://example.test/shifter"
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=[
            "authorization:installation.prepare_content",
            "authorization:installation.manage_adapters",
            "authorization:installation.read_content",
        ],
    )
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    return resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)


def test_service_preparation_records_original_proof_and_revocation_blocks_new_input(service_preparer, installed):
    from engine.models import PreparationAttempt, PreparationOperation
    from engine.services import get_artifact_preparation, read_preparation_worker_input, request_artifact_preparation
    from engine.services._preparation_worker import attempt_token
    from management.models import ServiceCredentialAdmission
    from shared.exceptions import ValidationError

    admitted = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    operation = PreparationOperation.objects.get(pk=admitted.id)
    assert operation.requested_by_id is None
    assert operation.initiating_principal_uuid == service_preparer.principal.uuid
    assert operation.credential_uuid == service_preparer.credential_uuid
    assert get_artifact_preparation(service_preparer, operation.id).state == "queued"
    attempt = PreparationAttempt.objects.get(operation=operation)
    token = attempt_token(attempt)
    assert read_preparation_worker_input(operation.id, token)["attempt_id"] == str(attempt.id)
    ServiceCredentialAdmission.objects.filter(uuid=service_preparer.credential_uuid).update(is_active=False)
    with pytest.raises(ValidationError):
        read_preparation_worker_input(operation.id, token)


@pytest.mark.parametrize("phase", ["verify-inputs", "build", "verify-output", "cleanup"])
def test_preparation_attempt_pins_an_independent_native_workload(service_preparer, installed, phase):
    from engine.models import PreparationAttempt, PreparationOperation
    from engine.services import request_artifact_preparation
    from engine.services._preparation_operations import new_preparation_attempt

    admitted = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    operation = PreparationOperation.objects.get(pk=admitted.id)
    attempt = PreparationAttempt.objects.get(operation=operation)
    if phase != attempt.phase:
        attempt = new_preparation_attempt(operation, phase)
    role = {"build": "builder", "verify-inputs": "verifier", "verify-output": "verifier", "cleanup": "cleanup"}[phase]
    assert attempt.executing_workload["kind"] == "kubernetes"
    assert attempt.executing_workload["namespace"] == operation.input["grant"]["namespace"]
    assert attempt.executing_workload["service_account_name"] == operation.input["grant"][f"{role}_service_account"]
    assert attempt.executing_workload["task_identity"] == f"preparation:{operation.id}:{attempt.id}"
    assert str(service_preparer.principal.uuid) not in str(attempt.executing_workload)
    assert operation.initiating_principal_uuid == service_preparer.principal.uuid


def test_preparation_attempt_native_workload_cannot_be_rebound(service_preparer, installed):
    from django.db import IntegrityError, transaction

    from engine.models import PreparationAttempt
    from engine.services import request_artifact_preparation

    operation = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PreparationAttempt.objects.filter(operation_id=operation.id).update(
            executing_workload={"kind": "kubernetes", "service_account_name": "portal"}
        )


def test_service_manages_preparation_adapter_installation_without_human_identity(service_preparer, grant):
    from engine.models import PreparationAdapter
    from engine.services import install_preparation_adapter, list_preparation_adapters, set_preparation_adapter_state
    from tests.shared.raes.test_preparation_contract import manifest_payload

    result = install_preparation_adapter(service_preparer, grant.id, manifest_payload())
    row = PreparationAdapter.objects.get(pk=result.id)
    assert row.installed_by_id is None
    assert row.installed_by_principal_uuid == service_preparer.principal.uuid
    assert {adapter.id for adapter in list_preparation_adapters(service_preparer)} == {row.id}
    assert set_preparation_adapter_state(service_preparer, row.id, "disabled").state == "disabled"


def test_preparation_only_ceiling_cannot_install_executable_adapter(service_preparer, grant):
    from engine.models import PreparationAdapter
    from engine.services import install_preparation_adapter
    from shared.authorization import CredentialCeiling
    from shared.credentials import CredentialContext
    from shared.exceptions import ValidationError
    from tests.shared.raes.test_preparation_contract import manifest_payload

    narrow = CredentialContext(
        service_preparer.principal,
        service_preparer.kind,
        service_preparer.credential_uuid,
        CredentialCeiling(frozenset({"installation.prepare_content"})),
        service_preparer.scopes,
    )
    with pytest.raises(ValidationError):
        install_preparation_adapter(narrow, grant.id, manifest_payload())
    assert not PreparationAdapter.objects.exists()


def test_service_can_cancel_and_retry_without_replacing_original_authority(service_preparer, installed):
    from engine.models import PreparationOperation
    from engine.services import cancel_artifact_preparation, request_artifact_preparation, retry_artifact_preparation
    from shared.exceptions import ValidationError

    operation = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    original = PreparationOperation.objects.get(pk=operation.id)
    assert cancel_artifact_preparation(service_preparer, operation.id).state == "cancelled"
    with pytest.raises(ValidationError):
        retry_artifact_preparation(service_preparer, operation.id)
    PreparationOperation.objects.filter(pk=operation.id).update(cleanup_pending=False)
    assert retry_artifact_preparation(service_preparer, operation.id).state == "queued"
    retried = PreparationOperation.objects.get(pk=operation.id)
    assert retried.credential_uuid == original.credential_uuid
    assert retried.input_digest == original.input_digest
    assert retried.current_attempt_id != original.current_attempt_id


@pytest.mark.django_db(transaction=True)
def test_dispatch_and_apply_policy_checks_never_hold_database_transaction(service_preparer, installed, monkeypatch):
    from django.db import connection

    from engine.models import PreparationOperation
    from engine.services import _preparation_controller as controller
    from engine.services import request_artifact_preparation
    from shared.authorization import port
    from tests.engine.services.test_preparation_controller import Observer, Task, receipt
    from tests.shared.raes.test_prepared_artifacts import admission_arguments

    operation = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    checked = []

    def check(request):
        assert not connection.in_atomic_block
        checked.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    task = Task()
    monkeypatch.setattr(controller, "get_preparation_task", lambda *args: task)
    monkeypatch.setattr(controller, "get_preparation_readback", lambda *args: Observer())
    controller.reconcile_preparation(operation.id)
    assert PreparationOperation.objects.get(pk=operation.id).state == "running"
    assert len(task.dispatched) == 1
    receipt(operation.id, admission_arguments()["inputs"])
    controller.reconcile_preparation(operation.id)
    assert PreparationOperation.objects.get(pk=operation.id).current_attempt_id is not None
    assert checked


def test_authentic_receipt_survives_revocation_but_cannot_advance_work(service_preparer, installed, monkeypatch):
    from engine.models import PreparationAttempt, PreparationOperation, PreparedArtifactAdmission
    from engine.services import _preparation_controller as controller
    from engine.services import request_artifact_preparation
    from management.models import ServiceCredentialAdmission
    from tests.engine.services.test_preparation_controller import Task, receipt
    from tests.shared.raes.test_prepared_artifacts import admission_arguments

    operation = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    attempt = PreparationAttempt.objects.get(operation_id=operation.id)
    ServiceCredentialAdmission.objects.filter(uuid=service_preparer.credential_uuid).update(is_active=False)
    receipt(operation.id, admission_arguments()["inputs"])
    task = Task()
    monkeypatch.setattr(controller, "get_preparation_task", lambda *args: task)
    controller.reconcile_preparation(operation.id)
    row = PreparationOperation.objects.get(pk=operation.id)
    assert row.state == "failed" and row.cleanup_pending
    attempt.refresh_from_db()
    assert attempt.result_digest
    assert not PreparedArtifactAdmission.objects.exists()
    assert not task.dispatched


@pytest.mark.parametrize(
    "field,value",
    [
        ("initiating_principal_uuid", uuid4()),
        ("credential_uuid", uuid4()),
        ("credential_ceiling_actions", "installation.manage_adapters"),
        ("authority_account_uuid", uuid4()),
        ("input", {}),
    ],
)
def test_persisted_preparation_proof_cannot_be_rewritten(service_preparer, installed, field, value):
    from django.db import IntegrityError, transaction

    from engine.models import PreparationOperation
    from engine.services import request_artifact_preparation

    operation = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PreparationOperation.objects.filter(pk=operation.id).update(**{field: value})


def test_source_prepared_inventory_retains_application_scope_and_is_not_a_global_catalog(
    service_preparer, installed, runtime
):
    from engine.models import PreparationOperation, PreparedArtifactAdmission
    from engine.services import list_backend_artifacts, request_artifact_preparation
    from tests.engine.services.test_preparation_controller import receipt
    from tests.shared.raes.test_prepared_artifacts import admission_arguments

    controller, _task, _observer = runtime
    view = request_artifact_preparation(
        service_preparer, installed.id, package_input(), scope=ResourceScope("installation")
    )
    evidence = admission_arguments()
    for phase, key in (("verify-inputs", "inputs"), ("build", "build"), ("verify-output", "output")):
        attempt = receipt(view.id, evidence[key])
        assert attempt.phase == phase
        controller.reconcile_preparation(view.id)
    operation = PreparationOperation.objects.get(pk=view.id)
    admission = PreparedArtifactAdmission.objects.get(operation=operation)
    assert admission.image_mapping.owner_principal_uuid == service_preparer.principal.uuid
    assert admission.image_mapping.scope_kind == "installation"
    assert list_backend_artifacts(provider="gce") == []
    scoped = list_backend_artifacts(provider="gce", credential=service_preparer, scope=ResourceScope("installation"))
    assert [row.image_ref for row in scoped] == [admission.image_mapping.image_ref]
