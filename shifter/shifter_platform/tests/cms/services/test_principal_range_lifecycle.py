"""Principal-owned lifecycle changes retain native queue and transaction semantics."""

from types import SimpleNamespace

import pytest

from cms.exceptions import CMSError
from shared.authorization import AuthorizationDecision, DecisionKind
from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["principal_range"]
pytestmark = [pytest.mark.django_db, pytest.mark.postgres]


@pytest.mark.parametrize("failed_deleted", [False, True])
def test_service_administrator_destroys_through_the_native_authority_outbox(lifecycle_range, failed_deleted):
    from django.utils import timezone

    from cms.services import destroy_range, destroy_range_by_request_id
    from engine.models import ProvisionerLaunchIntent

    fixture = lifecycle_range
    if failed_deleted:
        fixture.instance.status = fixture.realized.status = "failed"
        fixture.instance.deleted_at = timezone.now()
        fixture.instance.save(update_fields=["status", "deleted_at"])
        fixture.realized.save(update_fields=["status"])
    destroy_range(fixture.credential, fixture.instance.pk)
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "destroying"
    assert fixture.instance.deleted_at is not None
    queued = ProvisionerLaunchIntent.objects.get()
    assert queued.payload["operation"] == "destroy"
    assert queued.credential_uuid == fixture.credential.credential_uuid
    assert queued.initiating_principal_uuid == fixture.credential.principal.uuid
    assert queued.authority_owner_uuid == fixture.owner.uuid
    destroy_range_by_request_id(fixture.credential, str(fixture.request.request_id))
    assert ProvisionerLaunchIntent.objects.count() == 1


def test_native_destroy_entry_point_accepts_a_service_credential(lifecycle_range):
    from engine.services import destroy_range_by_request

    assert destroy_range_by_request(lifecycle_range.request.request_id, credential=lifecycle_range.credential)


def test_anonymous_native_destroy_cannot_mutate_principal_owned_range(lifecycle_range):
    from engine.models import ProvisionerLaunchIntent
    from engine.services import destroy_range_by_request

    with pytest.raises(PermissionError):
        destroy_range_by_request(lifecycle_range.request.request_id)
    lifecycle_range.realized.refresh_from_db()
    assert lifecycle_range.realized.status == "ready"
    assert not ProvisionerLaunchIntent.objects.exists()


def test_destroyed_range_cannot_reenter_the_principal_cleanup_queue(lifecycle_range):
    from django.utils import timezone

    from cms.services import destroy_range
    from engine.models import ProvisionerLaunchIntent

    fixture = lifecycle_range
    fixture.instance.status = fixture.realized.status = "destroyed"
    fixture.instance.deleted_at = timezone.now()
    fixture.instance.save(update_fields=["status", "deleted_at"])
    fixture.realized.save(update_fields=["status"])
    with pytest.raises(CMSError):
        destroy_range(fixture.credential, fixture.instance.pk)
    assert not ProvisionerLaunchIntent.objects.exists()


def test_source_destroy_audit_failure_rolls_back_both_projections_and_queue(lifecycle_range, monkeypatch):
    from cms.services import destroy_range
    from engine.models import OperationInput, ProvisionerLaunchIntent

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.services._scoped_range_lifecycle.audit_log", unavailable)
    with pytest.raises(CMSError):
        destroy_range(lifecycle_range.credential, lifecycle_range.instance.pk)
    lifecycle_range.instance.refresh_from_db()
    lifecycle_range.realized.refresh_from_db()
    assert lifecycle_range.instance.status == lifecycle_range.realized.status == "ready"
    assert lifecycle_range.instance.deleted_at is None
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()


def test_soft_deleted_teardown_rechecks_original_principal_before_dispatch(lifecycle_range):
    from cms.services import destroy_range
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    fixture = lifecycle_range
    destroy_range(fixture.credential, fixture.instance.pk)
    queued = ProvisionerLaunchIntent.objects.get()
    authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    with pytest.raises(PermissionError):
        authorize_provisioner_payload(queued.payload, expected_operation_id=queued.operation_id)


@pytest.fixture
def cancellable_range(lifecycle_range):
    from uuid import uuid4

    from engine.launch_intents import enqueue_provisioner_launch
    from management.models import ProviderBinding, ServiceCredentialAdmission
    from management.services import resolve_service_credential
    from tests.engine.test_launch_interrupt import _plan

    fixture = lifecycle_range
    admission = ServiceCredentialAdmission.objects.select_related("binding").get(
        uuid=fixture.credential.credential_uuid
    )
    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding, audience=admission.audience, scopes=["authorization:account.launch_range"]
    )
    initiator = resolve_service_credential(
        issuer=admission.binding.issuer, subject=admission.binding.subject, audience=admission.audience
    )
    fixture.instance.status = fixture.realized.status = "provisioning"
    fixture.instance.save(update_fields=["status"])
    fixture.realized.range_config = _plan()
    fixture.realized.save(update_fields=["status", "range_config"])
    enqueue_provisioner_launch(
        ["raes-range", "provision", "--request-id", str(fixture.request.request_id)], credential=initiator
    )
    return fixture


def test_direct_destroy_of_a_provisioning_range_waits_for_native_interrupt(cancellable_range):
    from cms.services import destroy_range
    from engine.models import InterruptState, ProvisionerLaunchIntent, RangeCancellationAuthority

    actor = cancellable_range
    origin = ProvisionerLaunchIntent.objects.get()
    destroy_range(actor.credential, actor.instance.pk)
    origin.refresh_from_db()
    actor.realized.refresh_from_db()
    assert origin.interrupt_state == InterruptState.REQUESTED
    assert actor.realized.provisioner_operation_id == origin.operation_id
    cancellation = RangeCancellationAuthority.objects.get(provision_intent=origin)
    assert cancellation.initiating_principal_uuid == actor.credential.principal.uuid
    assert not ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").exists()


def test_source_cancellation_preserves_initiator_and_interrupts_only_reserved_generation(cancellable_range):
    from cms.services import cancel_range
    from engine.models import InterruptState, ProvisionerLaunchIntent

    fixture = cancellable_range
    provision = ProvisionerLaunchIntent.objects.get()
    cancel_range(fixture.credential, fixture.instance.pk)
    provision.refresh_from_db()
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "destroying"
    assert provision.interrupt_state == InterruptState.REQUESTED
    assert fixture.realized.provisioner_operation_id == provision.operation_id
    assert ProvisionerLaunchIntent.objects.count() == 1
    cancellation = provision.cancellation
    assert cancellation.credential_uuid == fixture.credential.credential_uuid
    assert cancellation.credential_ceiling_actions != provision.credential_ceiling_actions
    assert cancellation.authority_owner_uuid == fixture.owner.uuid
    cancel_range(fixture.credential, fixture.instance.pk)
    assert provision.cancellation.pk == cancellation.pk


def test_cancellation_authority_cannot_be_rewritten(cancellable_range):
    from uuid import uuid4

    from django.db import IntegrityError, transaction

    from cms.services import cancel_range
    from engine.models import RangeCancellationAuthority

    cancel_range(cancellable_range.credential, cancellable_range.instance.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        RangeCancellationAuthority.objects.update(initiating_principal_uuid=uuid4())


def test_native_admission_revoked_during_policy_cannot_commit_destroy(lifecycle_range, monkeypatch):
    from cms.services import destroy_range
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission
    from shared.authorization import port

    checked = []

    def check(request):
        checked.append(request)
        if len(checked) == 2:
            ServiceCredentialAdmission.objects.filter(uuid=lifecycle_range.credential.credential_uuid).update(
                is_active=False
            )
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    with pytest.raises(CMSError):
        destroy_range(lifecycle_range.credential, lifecycle_range.instance.pk)
    lifecycle_range.instance.refresh_from_db()
    lifecycle_range.realized.refresh_from_db()
    assert lifecycle_range.instance.status == lifecycle_range.realized.status == "ready"
    assert not ProvisionerLaunchIntent.objects.exists()


def test_accepted_cancellation_cleanup_outlives_revoked_callers(cancellable_range, monkeypatch):
    from cms.services import cancel_range_by_request_id
    from engine.launch_intents import authorize_provisioner_payload
    from engine.launch_interrupt import drain_due_interrupts
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission
    from shared.cloud.types import TaskInterruptDisposition

    fixture = cancellable_range
    origin = ProvisionerLaunchIntent.objects.get()
    cancel_range_by_request_id(fixture.credential, str(fixture.request.request_id))
    ServiceCredentialAdmission.objects.filter(
        uuid__in=[origin.credential_uuid, fixture.credential.credential_uuid]
    ).update(is_active=False)
    monkeypatch.setattr(
        "engine.ecs.get_task_runner",
        lambda: SimpleNamespace(interrupt_task=lambda *args: TaskInterruptDisposition.TERMINAL_ABSENT),
    )
    assert drain_due_interrupts(1) == 1
    origin.refresh_from_db()
    assert origin.interrupt_state == InterruptState.DESTROY_ENQUEUED
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.cleanup_origin_operation_uuid == origin.operation_id
    assert cleanup.credential_uuid == origin.credential_uuid
    assert cleanup.authority_owner_uuid == fixture.owner.uuid
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)


def test_cancel_denied_caller_cannot_mark_the_provision_generation(cancellable_range):
    from cms.services import cancel_range
    from engine.models import InterruptState, ProvisionerLaunchIntent, RangeCancellationAuthority
    from management.models import ServiceCredentialAdmission

    fixture = cancellable_range
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    with pytest.raises(CMSError):
        cancel_range(fixture.credential, fixture.instance.pk)
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "provisioning"
    assert ProvisionerLaunchIntent.objects.get().interrupt_state == InterruptState.NONE
    assert not RangeCancellationAuthority.objects.exists()


def test_cancel_audit_failure_rolls_back_interrupt_and_cancellation_proof(cancellable_range, monkeypatch):
    from cms.services import cancel_range
    from engine.models import InterruptState, ProvisionerLaunchIntent, RangeCancellationAuthority

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    monkeypatch.setattr("cms.services._scoped_range_lifecycle.audit_log", unavailable)
    with pytest.raises(CMSError):
        cancel_range(cancellable_range.credential, cancellable_range.instance.pk)
    cancellable_range.instance.refresh_from_db()
    cancellable_range.realized.refresh_from_db()
    assert cancellable_range.instance.status == cancellable_range.realized.status == "provisioning"
    assert ProvisionerLaunchIntent.objects.get().interrupt_state == InterruptState.NONE
    assert not RangeCancellationAuthority.objects.exists()


def test_expired_principal_range_uses_explicit_cleanup_after_initiator_revocation(lifecycle_range):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services import pause_range
    from cms.services._range_destroy import destroy_expired_range
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    fixture = lifecycle_range
    pause_range(fixture.credential, fixture.instance.pk)
    origin = ProvisionerLaunchIntent.objects.get()
    fixture.instance.status = fixture.realized.status = "ready"
    fixture.instance.expires_at = timezone.now() - timedelta(seconds=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["status", "expires_at", "maximum_expires_at", "extension_days"])
    fixture.realized.save(update_fields=["status"])
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    destroy_expired_range(fixture.instance)
    cleanup = ProvisionerLaunchIntent.objects.get(payload__operation="destroy")
    assert cleanup.authority_kind == "cleanup"
    assert cleanup.cleanup_origin_operation_uuid == origin.operation_id
    authorize_provisioner_payload(cleanup.payload, expected_operation_id=cleanup.operation_id)
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "destroying"
    destroy_expired_range(fixture.instance)
    assert ProvisionerLaunchIntent.objects.filter(payload__operation="destroy").count() == 1


def test_expired_provisioning_range_records_interrupt_before_teardown(cancellable_range):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services._range_destroy import destroy_expired_range
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    fixture = cancellable_range
    origin = ProvisionerLaunchIntent.objects.get()
    fixture.instance.expires_at = timezone.now() - timedelta(seconds=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["expires_at", "maximum_expires_at", "extension_days"])
    ServiceCredentialAdmission.objects.filter(
        uuid__in=[origin.credential_uuid, fixture.credential.credential_uuid]
    ).update(is_active=False)
    destroy_expired_range(fixture.instance)
    origin.refresh_from_db()
    assert origin.interrupt_state == InterruptState.REQUESTED
    assert ProvisionerLaunchIntent.objects.count() == 1
    assert origin.cancellation.authority_kind == "cleanup"


def test_server_cleanup_rejects_an_in_memory_expiry_override(lifecycle_range):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services._range_destroy import destroy_expired_range
    from engine.models import ProvisionerLaunchIntent

    fixture = lifecycle_range
    fixture.instance.expires_at = timezone.now() + timedelta(days=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["expires_at", "maximum_expires_at", "extension_days"])
    fixture.instance.expires_at = timezone.now() - timedelta(days=1)
    with pytest.raises(CMSError):
        destroy_expired_range(fixture.instance)
    fixture.instance.refresh_from_db()
    assert fixture.instance.status == "ready"
    assert not ProvisionerLaunchIntent.objects.exists()


def test_expiry_cleanup_can_replace_a_revoked_callers_queued_destroy(lifecycle_range):
    from datetime import timedelta

    from django.utils import timezone

    from cms.services import destroy_range
    from cms.services._range_destroy import destroy_expired_range
    from engine.models import ProvisionerLaunchIntent
    from management.models import ServiceCredentialAdmission

    fixture = lifecycle_range
    destroy_range(fixture.credential, fixture.instance.pk)
    origin = ProvisionerLaunchIntent.objects.get()
    fixture.instance.refresh_from_db()
    fixture.instance.expires_at = timezone.now() - timedelta(seconds=1)
    fixture.instance.maximum_expires_at = fixture.instance.expires_at + timedelta(days=1)
    fixture.instance.extension_days = 1
    fixture.instance.save(update_fields=["expires_at", "maximum_expires_at", "extension_days"])
    ServiceCredentialAdmission.objects.filter(uuid=fixture.credential.credential_uuid).update(is_active=False)
    destroy_expired_range(fixture.instance)
    cleanup = ProvisionerLaunchIntent.objects.get(authority_kind="cleanup")
    assert cleanup.operation_id != origin.operation_id
    assert cleanup.cleanup_origin_operation_uuid == origin.operation_id


@pytest.fixture
def lifecycle_range(principal_range, settings):
    settings.LOCAL_PROVISIONER = None
    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic@sha256:" + "1" * 64
    return principal_range


def test_service_administrator_pauses_and_resumes_through_the_native_outbox(lifecycle_range):
    from cms.services import pause_range, resume_range_by_request_id
    from engine.models import ProvisionerLaunchIntent
    from shared.models import AuditLog

    fixture = lifecycle_range
    pause_range(fixture.credential, fixture.instance.pk)
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "pausing"
    queued = ProvisionerLaunchIntent.objects.get()
    assert queued.payload["operation"] == "pause"
    assert queued.credential_uuid == fixture.credential.credential_uuid
    assert queued.initiating_principal_uuid == fixture.credential.principal.uuid
    assert queued.authority_owner_uuid == fixture.owner.uuid
    pause_range(fixture.credential, fixture.instance.pk)
    assert ProvisionerLaunchIntent.objects.count() == 1
    fixture.instance.status = fixture.realized.status = "paused"
    fixture.instance.save(update_fields=["status"])
    fixture.realized.save(update_fields=["status"])
    resume_range_by_request_id(fixture.credential, str(fixture.request.request_id))
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "resuming"
    resumed = ProvisionerLaunchIntent.objects.get(payload__operation="resume")
    assert resumed.operation_id != queued.operation_id
    assert resumed.credential_uuid == fixture.credential.credential_uuid
    assert AuditLog.objects.filter(actor_principal_uuid=fixture.credential.principal.uuid, action="pause").exists()


def test_engine_lifecycle_commits_its_independently_authorized_range(lifecycle_range):
    from engine.services import commit_prepared_range_lifecycle, prepare_range_lifecycle

    prepared = prepare_range_lifecycle(lifecycle_range.credential, lifecycle_range.request.request_id, "pause")
    assert commit_prepared_range_lifecycle(prepared)


def test_anonymous_engine_lifecycle_call_cannot_mutate_principal_owned_range(lifecycle_range):
    from engine.models import ProvisionerLaunchIntent
    from engine.services import pause_range

    with pytest.raises(PermissionError):
        pause_range(lifecycle_range.request.request_id)
    lifecycle_range.realized.refresh_from_db()
    assert lifecycle_range.realized.status == "ready"
    assert not ProvisionerLaunchIntent.objects.exists()


def test_native_pause_entry_point_accepts_a_service_credential(lifecycle_range):
    from engine.services import pause_range

    assert pause_range(lifecycle_range.request.request_id, credential=lifecycle_range.credential)


def test_source_cms_audit_failure_rolls_back_both_projections_and_native_queue(lifecycle_range, monkeypatch):
    from cms.services import pause_range
    from engine.models import OperationInput, ProvisionerLaunchIntent
    from shared.models import AuditLog

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic audit outage")

    before = AuditLog.objects.count()
    monkeypatch.setattr("cms.services._scoped_range_lifecycle.audit_log", unavailable)
    with pytest.raises(CMSError):
        pause_range(lifecycle_range.credential, lifecycle_range.instance.pk)
    lifecycle_range.instance.refresh_from_db()
    lifecycle_range.realized.refresh_from_db()
    assert lifecycle_range.instance.status == lifecycle_range.realized.status == "ready"
    assert lifecycle_range.realized.provisioner_operation_id is None
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()
    assert AuditLog.objects.count() == before


def test_source_lifecycle_denial_has_no_state_queue_or_audit_effect(lifecycle_range):
    from cms.services import pause_range
    from engine.models import ProvisionerLaunchIntent
    from shared.authorization import CredentialCeiling
    from shared.credentials import CredentialContext
    from shared.models import AuditLog

    fixture = lifecycle_range
    narrow = CredentialContext(
        fixture.credential.principal,
        fixture.credential.kind,
        fixture.credential.credential_uuid,
        CredentialCeiling(frozenset({"range.read"})),
        fixture.credential.scopes,
    )
    before = AuditLog.objects.count()
    with pytest.raises(CMSError):
        pause_range(narrow, fixture.instance.pk)
    fixture.instance.refresh_from_db()
    fixture.realized.refresh_from_db()
    assert fixture.instance.status == fixture.realized.status == "ready"
    assert not ProvisionerLaunchIntent.objects.exists()
    assert AuditLog.objects.count() == before


def test_native_admission_cannot_adopt_resource_changes_made_during_policy(lifecycle_range, monkeypatch):
    from uuid import uuid4

    from engine.launch_intents import enqueue_provisioner_launch
    from engine.models import ProvisionerLaunchIntent, Range
    from shared.authorization import port

    fixture = lifecycle_range
    fixture.realized.status = "resuming"
    fixture.realized.save(update_fields=["status"])

    def check(request):
        Range.objects.filter(pk=fixture.realized.pk).update(owner_principal_uuid=uuid4())
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    with pytest.raises((ValueError, PermissionError)):
        enqueue_provisioner_launch(
            ["range", "resume", "--request-id", str(fixture.request.request_id)], credential=fixture.credential
        )
    assert not ProvisionerLaunchIntent.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_source_lifecycle_policy_is_evaluated_before_sql_critical_sections(lifecycle_range, monkeypatch):
    from django.db import connection

    from cms.services import pause_range
    from shared.authorization import port

    checked = []

    def check(request):
        assert not connection.in_atomic_block
        checked.append(request)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    pause_range(lifecycle_range.credential, lifecycle_range.instance.pk)
    assert checked
