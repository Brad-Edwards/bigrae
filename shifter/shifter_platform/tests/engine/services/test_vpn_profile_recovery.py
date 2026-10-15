"""Failure convergence uses native operation evidence without replacing proof."""

from datetime import timedelta

import pytest
from django.utils import timezone

from tests.engine.services.test_principal_vpn import confirm_issuer_absence, principal_range, vpn_range

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]
__all__ = ["principal_range", "vpn_range"]


@pytest.mark.parametrize("operation", ["refresh-crl", "revoke", "purge"])
def test_dispatched_maintenance_without_receipt_waits_for_absence_then_recovers(vpn_range, monkeypatch, operation):
    from uuid import uuid4

    from django.db import transaction

    from engine.launch_interrupt import drain_due_interrupts
    from engine.models import InterruptState, OperationInput, OperationResultInbox, ProvisionerLaunchIntent
    from engine.services._vpn_profile_operations import authorize_vpn_operation, queue_vpn_operation
    from engine.services._vpn_profiles import (
        reconcile_vpn_profiles,
        request_vpn_issuance_interrupt,
        request_vpn_profile,
    )
    from shared.operation_envelope import build_operation_envelope, canonical_payload_digest

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    row.state, row.certificate_serial = "ready", "1234"
    row.save(update_fields=["state", "certificate_serial"])
    if operation == "refresh-crl":
        queue_vpn_operation(row, operation)
    else:
        row.state, row.revoked_at = "revoked", timezone.now()
        row.save(update_fields=["state", "revoked_at"])
        with transaction.atomic():
            request_vpn_issuance_interrupt(row)
        confirm_issuer_absence(monkeypatch)
        row.refresh_from_db()
        if operation == "purge":
            # Accepted revocation of an expired certificate is sufficient purge
            # evidence; CRL signature/receipt application has separate coverage.
            payload = {"profile_id": str(row.pk)}
            envelope = build_operation_envelope(
                operation_id=row.provisioner_operation_id,
                request_id=row.generation,
                resource="vpn-profile",
                operation="revoke",
                payload=payload,
            )
            OperationResultInbox.objects.create(
                operation_id=row.provisioner_operation_id,
                request_id=row.generation,
                resource="vpn-profile",
                operation="revoke",
                contract_version=envelope["contract_version"],
                result_kind="TERMINAL_SUCCESS",
                result_step="vpn_pki_ready",
                result_identity=str(uuid4()),
                payload_digest=canonical_payload_digest(payload),
                envelope=envelope,
                disposition="APPLIED",
            )
            row.revocation_operation_id = row.provisioner_operation_id
            row.save(update_fields=["revocation_operation_id"])
            later = row.expires_at + timedelta(seconds=1)
            monkeypatch.setattr("engine.services._vpn_profiles.timezone.now", lambda: later)
            queue_vpn_operation(row, operation)
    origin = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    original_input = OperationInput.objects.get(operation_id=origin.operation_id).envelope["payload"]
    origin.status, origin.launched_at = "SUCCEEDED", timezone.now() - timedelta(minutes=16)
    origin.save(update_fields=["status", "launched_at"])
    reconcile_vpn_profiles(limit=10)
    origin.refresh_from_db()
    row.refresh_from_db()
    assert origin.interrupt_state == InterruptState.REQUESTED
    assert row.provisioner_operation_id == origin.operation_id
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    replacement = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    assert replacement.operation_id != origin.operation_id
    assert replacement.payload["operation"] == operation
    assert replacement.cleanup_origin_operation_uuid == row.issuance_operation_id
    assert replacement.credential_uuid == origin.credential_uuid
    replacement_input = OperationInput.objects.get(operation_id=replacement.operation_id).envelope["payload"]
    for key in ("storage_project_id", "storage_environment", "issuer_secret_ref"):
        assert replacement_input[key] == original_input[key]
    authorize_vpn_operation(replacement.payload, replacement.operation_id)
    assert drain_due_interrupts(1) == 0


@pytest.mark.parametrize("dispatch", ["exhausted", "missing_receipt"])
def test_failed_or_unconfirmed_issuance_converges_to_native_interrupt(vpn_range, dispatch):
    from engine.models import InterruptState, ProvisionerLaunchIntent, ProvisionerLaunchStatus
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    origin = ProvisionerLaunchIntent.objects.get(operation_id=row.issuance_operation_id)
    origin.task_ref = "synthetic-namespace/synthetic-issuance"
    origin.status = ProvisionerLaunchStatus.DLQ if dispatch == "exhausted" else ProvisionerLaunchStatus.SUCCEEDED
    origin.launched_at = timezone.now() - timedelta(minutes=16)
    origin.save(update_fields=["task_ref", "status", "launched_at"])
    assert reconcile_vpn_profiles(limit=10) == 1
    row.refresh_from_db()
    origin.refresh_from_db()
    assert row.state == "revoked"
    assert origin.interrupt_state == InterruptState.REQUESTED


def test_revocation_interrupts_an_active_crl_refresh_before_replacement(vpn_range, monkeypatch):
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from engine.services._vpn_profile_operations import queue_vpn_operation
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    row.state, row.certificate_serial = "ready", "1234"
    row.save(update_fields=["state", "certificate_serial"])
    queue_vpn_operation(row, "refresh-crl")
    refresh = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    reconcile_vpn_profiles(limit=10)
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    refresh.refresh_from_db()
    assert row.provisioner_operation_id == refresh.operation_id
    assert refresh.interrupt_state == InterruptState.REQUESTED
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    assert row.provisioner_operation == "revoke"


@pytest.mark.parametrize("failure", ["dispatch_dlq", "terminal_failure"])
def test_failed_revocation_is_retried_under_the_same_confirmed_cleanup_origin(vpn_range, monkeypatch, failure):
    from uuid import uuid4

    from engine.models import InterruptState, OperationResultInbox, ProvisionerLaunchIntent, ProvisionerLaunchStatus
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    reconcile_vpn_profiles(limit=10)
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    failed = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    assert failed.payload["operation"] == "revoke"
    if failure == "dispatch_dlq":
        failed.status = ProvisionerLaunchStatus.DLQ
        failed.save(update_fields=["status"])
    else:
        from shared.operation_envelope import build_operation_envelope, canonical_payload_digest

        payload = {"reason_code": "cloud_operation_failed"}
        envelope = build_operation_envelope(
            operation_id=failed.operation_id,
            request_id=row.generation,
            resource="vpn-profile",
            operation="revoke",
            payload=payload,
        )
        OperationResultInbox.objects.create(
            operation_id=failed.operation_id,
            request_id=row.generation,
            resource="vpn-profile",
            operation="revoke",
            result_kind="TERMINAL_FAILURE",
            result_step="vpn_pki_failed",
            result_identity=str(uuid4()),
            contract_version=envelope["contract_version"],
            payload_digest=canonical_payload_digest(payload),
            envelope=envelope,
            disposition="APPLIED",
        )
    reconcile_vpn_profiles(limit=10)
    failed.refresh_from_db()
    assert failed.interrupt_state == InterruptState.REQUESTED
    row.refresh_from_db()
    assert row.provisioner_operation_id == failed.operation_id
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    assert row.provisioner_operation_id != failed.operation_id
    retried = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    assert retried.cleanup_origin_operation_uuid == row.issuance_operation_id
    assert retried.credential_uuid == failed.credential_uuid


def test_cleanup_keeps_issuance_storage_binding_after_configuration_changes(vpn_range, settings, monkeypatch):
    from engine.models import OperationInput
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission

    settings.GCP_DYNAMIC_SECRET_PROJECT_ID = "recorded-secrets"
    settings.ENVIRONMENT = "recorded-env"
    monkeypatch.setenv("RANGE_OPENVPN_ISSUER_SECRET_ID", "projects/recorded-platform/secrets/issuer")
    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    settings.GCP_DYNAMIC_SECRET_PROJECT_ID = "changed-secrets"
    settings.ENVIRONMENT = "changed-env"
    monkeypatch.setenv("RANGE_OPENVPN_ISSUER_SECRET_ID", "projects/changed-platform/secrets/issuer")
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    reconcile_vpn_profiles(limit=10)
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    value = OperationInput.objects.get(operation_id=row.provisioner_operation_id).envelope["payload"]
    assert value["storage_project_id"] == "recorded-secrets"
    assert value["storage_environment"] == "recorded-env"
    assert value["issuer_secret_ref"] == "projects/recorded-platform/secrets/issuer"
