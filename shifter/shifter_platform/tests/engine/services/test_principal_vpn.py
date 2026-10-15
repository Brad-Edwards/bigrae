"""A verified VPN profile cannot borrow another application credential."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from tests.cms.services.test_principal_range_authority import principal_range

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgres]
__all__ = ["principal_range"]


@pytest.fixture
def vpn_range(principal_range, settings):
    from shared.remote_access import build_openvpn_capability

    settings.ENGINE_TASK_CLUSTER = "synthetic-namespace"
    settings.ENGINE_TASK_DEFINITION = "synthetic-provisioner:1"
    row = principal_range.realized
    target = "provision.node.client#0"
    row.remote_access_capability = build_openvpn_capability(target, timezone.now() + timedelta(hours=1))
    row.vpn_access_binding = {"endpoint": "203.0.113.7", "port": 1194}
    row.provisioned_instances = [
        {"uuid": target, "role": "raes-node", "private_ip": "10.50.3.3", "participant_access_channels": ["ssh"]}
    ]
    row.save(update_fields=["remote_access_capability", "provisioned_instances", "vpn_access_binding"])
    return principal_range


def profile(fixture):
    from engine.models import VpnAccessProfile
    from engine.services._vpn_profiles import admit_vpn_profile_authority

    return VpnAccessProfile.objects.create(
        range=fixture.realized,
        generation=fixture.request.request_id,
        resource_generation=fixture.realized.resource_generation,
        expires_at=timezone.now() + timedelta(minutes=30),
        state="ready",
        certificate_serial="1234",
        certificate_fingerprint="ab" * 32,
        issuer_fingerprint="cd" * 32,
        secret_ref="synthetic-profile-reference",
        **admit_vpn_profile_authority(fixture.credential, fixture.request.request_id),
    )


def connect(row, *, fingerprint=None):
    from engine.services import authorize_vpn_session

    return authorize_vpn_session(
        common_name=f"access-{row.pk}",
        server="vpn-a",
        client_id=7,
        certificate_serial=row.certificate_serial,
        certificate_fingerprint=fingerprint or row.certificate_fingerprint,
    )


def confirm_issuer_absence(monkeypatch):
    """Exercise the existing interrupt worker through its external native port."""
    from types import SimpleNamespace

    from engine.launch_interrupt import drain_due_interrupts
    from shared.cloud.types import TaskInterruptDisposition

    monkeypatch.setattr(
        "engine.ecs.get_task_runner",
        lambda: SimpleNamespace(interrupt_task=lambda *_args: TaskInterruptDisposition.TERMINAL_ABSENT),
    )
    assert drain_due_interrupts(1) == 1


def test_individual_profile_admits_administrator_without_impersonating_range_owner(vpn_range):
    from engine.models import VpnSession

    row = profile(vpn_range)
    grant = connect(row)
    session = VpnSession.objects.get(pk=grant.session_id)
    assert grant.target_ip == "10.50.3.3"
    assert session.profile_id == row.pk
    assert row.initiating_principal_uuid != vpn_range.owner.uuid


def test_revoked_original_credential_cannot_borrow_later_live_credential(vpn_range):
    from engine.services import VpnSessionDenied
    from management.models import ServiceCredentialAdmission

    row = profile(vpn_range)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    with pytest.raises(VpnSessionDenied):
        connect(row)


def test_certificate_with_valid_common_name_but_different_key_is_denied(vpn_range):
    from engine.services import VpnSessionDenied

    row = profile(vpn_range)
    with pytest.raises(VpnSessionDenied):
        connect(row, fingerprint="ef" * 32)


def test_idle_session_is_returned_for_native_termination_after_credential_revocation(vpn_range):
    from engine.services import renew_vpn_sessions
    from management.models import ServiceCredentialAdmission

    row = profile(vpn_range)
    grant = connect(row)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    assert renew_vpn_sessions(server="vpn-a", session_ids=[str(grant.session_id)]) == (grant.session_id,)


def test_changed_runtime_generation_cannot_reuse_profile(vpn_range):
    from engine.services import VpnSessionDenied

    row = profile(vpn_range)
    vpn_range.realized.resource_generation = uuid4()
    vpn_range.realized.save(update_fields=["resource_generation"])
    with pytest.raises(VpnSessionDenied):
        connect(row)


def test_profile_authorization_runs_outside_business_transaction(vpn_range, monkeypatch):
    from django.db import connection

    from shared.authorization import port

    row = profile(vpn_range)
    provider = port.configured_authorization_provider()
    check = provider.check

    def unlocked_check(request):
        assert not connection.in_atomic_block
        return check(request)

    monkeypatch.setattr(
        port, "_provider_factory", lambda: type("Provider", (), {"check": staticmethod(unlocked_check)})()
    )
    grant = connect(row)
    from engine.services import renew_vpn_sessions

    assert renew_vpn_sessions(server="vpn-a", session_ids=[str(grant.session_id)]) == ()


def test_profile_request_queues_once_without_mutating_range_operation(vpn_range):
    from engine.models import OperationInput, ProvisionerLaunchIntent, VpnAccessProfile
    from engine.services._vpn_profiles import request_vpn_profile

    fixture = vpn_range
    first = request_vpn_profile(fixture.credential, fixture.request.request_id)
    second = request_vpn_profile(fixture.credential, fixture.request.request_id)
    assert first.pk == second.pk
    assert VpnAccessProfile.objects.count() == 1
    assert ProvisionerLaunchIntent.objects.count() == 1
    assert OperationInput.objects.count() == 1
    fixture.realized.refresh_from_db()
    assert fixture.realized.provisioner_operation_id is None
    row = ProvisionerLaunchIntent.objects.get()
    assert row.credential_uuid == fixture.credential.credential_uuid
    assert row.payload["profile_id"] == str(first.pk)
    assert "private_key" not in str(OperationInput.objects.get().envelope)


def test_queued_issuance_cannot_dispatch_after_original_credential_revocation(vpn_range):
    from engine.launch_intents import authorize_provisioner_payload
    from engine.models import ProvisionerLaunchIntent
    from engine.services._vpn_profiles import request_vpn_profile
    from management.models import ServiceCredentialAdmission

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    intent = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    with pytest.raises(PermissionError):
        authorize_provisioner_payload(intent.payload, expected_operation_id=intent.operation_id)


def test_bulk_write_cannot_replace_original_profile_credential(vpn_range):
    from django.db import IntegrityError, transaction

    from engine.models import VpnAccessProfile

    row = profile(vpn_range)
    with pytest.raises(IntegrityError), transaction.atomic():
        VpnAccessProfile.objects.filter(pk=row.pk).update(credential_uuid=uuid4())


@pytest.mark.parametrize("operation", ["issue", "revoke", "issue_revoked"])
def test_native_result_inbox_applies_certificate_receipt_and_signed_crl(vpn_range, operation):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from engine.models import OperationInput, OperationResultInbox, VpnCrlState
    from engine.services import apply_pending_operation_results
    from engine.services._vpn_profiles import request_vpn_profile
    from shared.operation_envelope import build_operation_envelope, canonical_payload_digest

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    retired_issue = operation == "issue_revoked"
    if retired_issue:
        operation = "issue"
        row.state, row.revoked_at = "revoked", timezone.now()
        row.save(update_fields=["state", "revoked_at"])
    if operation == "revoke":
        from django.db import transaction

        from engine.models import ProvisionerLaunchIntent
        from engine.services._vpn_profiles import request_vpn_issuance_interrupt

        with transaction.atomic():
            row.state, row.revoked_at = "revoked", timezone.now()
            row.save(update_fields=["state", "revoked_at"])
            ProvisionerLaunchIntent.objects.filter(operation_id=row.issuance_operation_id).update(task_ref="")
            request_vpn_issuance_interrupt(row)
    value = OperationInput.objects.get(operation_id=row.provisioner_operation_id).envelope["payload"]
    now = timezone.now()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(name)
        .last_update(now)
        .next_update(now + timedelta(hours=24))
        .add_extension(x509.CRLNumber(value["crl_revision"]), critical=False)
    )
    if operation == "revoke":
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(1234).revocation_date(now).build()
        )
    crl = builder.sign(key, hashes.SHA256())
    receipt = {
        "profile_id": str(row.pk),
        "issuer_certificate": ca.public_bytes(serialization.Encoding.PEM).decode(),
        "issuer_fingerprint": ca.fingerprint(hashes.SHA256()).hex(),
        "certificate_serial": "1234",
        "certificate_fingerprint": "ab" * 32,
        "secret_ref": "projects/synthetic-project/secrets/profile",
        "crl_revision": value["crl_revision"],
        "crl": crl.public_bytes(serialization.Encoding.PEM).decode(),
    }
    envelope = build_operation_envelope(
        operation_id=row.provisioner_operation_id,
        request_id=row.generation,
        resource="vpn-profile",
        operation=operation,
        payload=receipt,
    )
    result = OperationResultInbox.objects.create(
        operation_id=row.provisioner_operation_id,
        request_id=row.generation,
        resource="vpn-profile",
        operation=operation,
        contract_version=envelope["contract_version"],
        result_kind="TERMINAL_SUCCESS",
        result_step="vpn_pki_ready",
        result_identity=str(uuid4()),
        payload_digest=canonical_payload_digest(receipt),
        envelope=envelope,
    )
    assert apply_pending_operation_results() == 1
    row.refresh_from_db()
    result.refresh_from_db()
    assert result.disposition == "APPLIED"
    assert row.state == ("ready" if operation == "issue" and not retired_issue else "revoked")
    assert row.certificate_serial == "1234"
    assert VpnCrlState.objects.get(pk=1).published_revision == (0 if retired_issue else value["crl_revision"])
    if operation == "revoke":
        from engine.services._vpn_profiles import reconcile_vpn_profiles

        reconcile_vpn_profiles(limit=10)
        row.refresh_from_db()
        assert row.provisioner_operation == "purge"
        from engine.models import ProvisionerLaunchIntent
        from engine.services._vpn_profile_operations import authorize_vpn_operation

        intent = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
        authorize_vpn_operation(intent.payload, intent.operation_id)
        purge_value = OperationInput.objects.get(operation_id=row.provisioner_operation_id).envelope["payload"]
        purge_crl = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(name)
            .last_update(now)
            .next_update(now + timedelta(hours=24))
            .add_extension(x509.CRLNumber(purge_value["crl_revision"]), critical=False)
            .add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(1234).revocation_date(now).build())
            .sign(key, hashes.SHA256())
        )
        purge_receipt = {
            **receipt,
            "certificate_serial": "",
            "certificate_fingerprint": "",
            "secret_ref": "",
            "crl_revision": purge_value["crl_revision"],
            "crl": purge_crl.public_bytes(serialization.Encoding.PEM).decode(),
        }
        purge_envelope = build_operation_envelope(
            operation_id=row.provisioner_operation_id,
            request_id=row.generation,
            resource="vpn-profile",
            operation="purge",
            payload=purge_receipt,
        )
        OperationResultInbox.objects.create(
            operation_id=row.provisioner_operation_id,
            request_id=row.generation,
            resource="vpn-profile",
            operation="purge",
            contract_version=purge_envelope["contract_version"],
            result_kind="TERMINAL_SUCCESS",
            result_step="vpn_pki_ready",
            result_identity=str(uuid4()),
            payload_digest=canonical_payload_digest(purge_receipt),
            envelope=purge_envelope,
        )
        assert apply_pending_operation_results() == 1
        row.refresh_from_db()
        assert row.secret_removed_at is not None


def test_first_source_download_queues_preparation_without_delivering_shared_key(vpn_range, monkeypatch):
    from engine.models import VpnAccessProfile
    from engine.services import get_openvpn_profile
    from engine.services._vpn import VpnProfilePending

    reads = []
    monkeypatch.setattr("engine.services._vpn.get_openvpn_profile_secret", lambda reference: reads.append(reference))
    with pytest.raises(VpnProfilePending):
        get_openvpn_profile(vpn_range.credential, vpn_range.request.request_id)
    assert reads == []
    assert VpnAccessProfile.objects.count() == 1


def test_permanent_credential_revocation_retires_profile_and_queues_native_crl_cleanup(vpn_range, monkeypatch):
    from engine.models import ProvisionerLaunchIntent
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    row.state = "ready"
    row.certificate_serial = "1234"
    row.certificate_fingerprint = "ab" * 32
    row.issuer_fingerprint = "cd" * 32
    row.secret_ref = "projects/synthetic-project/secrets/profile"
    row.save(
        update_fields=["state", "certificate_serial", "certificate_fingerprint", "issuer_fingerprint", "secret_ref"]
    )
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    assert reconcile_vpn_profiles(limit=10) == 1
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    assert row.state == "revoked"
    assert row.revoked_at is not None
    intent = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    assert intent.payload["operation"] == "revoke"
    assert intent.authority_kind == "cleanup"
    assert intent.cleanup_origin_operation_uuid == row.issuance_operation_id
    assert intent.credential_uuid == row.credential_uuid


def test_source_vpn_realization_binds_business_principal_without_fabricated_user(vpn_range):
    from shared.remote_access import bind_openvpn_realization, parse_openvpn_binding

    fixture = vpn_range
    bound = bind_openvpn_realization(
        {
            "generation": str(fixture.request.request_id),
            "target_ref": "provision.node.client#0",
            "endpoint": "203.0.113.7",
            "port": 1194,
            "secret_ref": "synthetic-profile-reference",
        },
        owner_principal_uuid=fixture.owner.uuid,
    )
    parsed = parse_openvpn_binding(bound)
    assert parsed.owner_principal_uuid == fixture.owner.uuid
    assert parsed.owner_user_id is None
    assert "owner_user_id" not in bound


def test_service_profile_api_prepares_explicit_authorized_range_without_user_impersonation(vpn_range):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from mission_control.api._vpn import MissionControlVpnProfileView

    request = APIRequestFactory().post(
        f"/api/v1/mission-control/range/vpn-profile/?request_id={vpn_range.request.request_id}",
    )
    force_authenticate(request, user=None, token=vpn_range.credential)
    response = MissionControlVpnProfileView.as_view()(request)
    assert response.status_code == 202
    assert response.data["error"]["code"] == "vpn_preparing"
    assert response["Retry-After"] == "5"


def test_ctf_service_profile_preserves_participation_decision_and_live_assignment(
    vpn_range, django_user_model, settings
):
    from ctf.exceptions import CTFVpnProfilePending
    from ctf.models import CTFEvent, CTFParticipant
    from ctf.services.range.vpn import get_vpn_profile
    from engine.models import VpnAccessProfile
    from management.models import ServiceCredentialAdmission
    from management.services import resolve_service_credential_reference

    fixture = vpn_range
    admission = ServiceCredentialAdmission.objects.get(uuid=fixture.credential.credential_uuid)
    from management.models import ProviderBinding

    binding = ProviderBinding.objects.create(
        principal=admission.binding.principal, issuer=admission.binding.issuer, subject=str(uuid4())
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding, audience=admission.audience, scopes=[*admission.scopes, "authorization:event.participate"]
    )
    credential = resolve_service_credential_reference(admission.uuid, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE)
    creator = django_user_model.objects.create_user(username="synthetic-vpn-event-creator")
    event = CTFEvent.objects.create(
        name="Synthetic VPN event",
        created_by=creator,
        scope_kind="account",
        account_id=fixture.account.id,
        status="active",
        scenario_id="synthetic",
        event_start=timezone.now() - timedelta(minutes=1),
        event_end=timezone.now() + timedelta(hours=1),
    )
    participant = CTFParticipant.objects.create(
        event=event,
        principal_uuid=credential.principal.uuid,
        name="Synthetic VPN participant",
        status="active",
        registered_at=timezone.now(),
        range_instance_id=fixture.instance.pk,
    )
    fixture.instance.event_uuid, fixture.instance.range_source = event.pk, "ctf"
    fixture.instance.save(update_fields=["event_uuid", "range_source"])
    fixture.realized.event_uuid = event.pk
    fixture.realized.save(update_fields=["event_uuid"])
    with pytest.raises(CTFVpnProfilePending):
        get_vpn_profile(participant.pk, credential=credential)
    from rest_framework.test import APIRequestFactory, force_authenticate

    from ctf.api.organizer.ranges import ParticipantVpnProfileView

    request = APIRequestFactory().post(f"/api/v1/ctf/range/vpn-profile/?event_id={event.pk}")
    force_authenticate(request, user=None, token=credential)
    response = ParticipantVpnProfileView.as_view()(request)
    assert response.status_code == 202
    assert response.data["error"]["code"] == "vpn_preparing"
    assert response["Retry-After"] == "5"
    row = VpnAccessProfile.objects.get()
    assert row.authority_action == "event.participate"
    assert row.authority_target_uuid == event.pk
    participant.range_instance_id = None
    participant.save(update_fields=["range_instance_id"])
    with pytest.raises(ValueError, match="Participation denied"):
        get_vpn_profile(participant.pk, credential=credential)


def test_vpn_session_cannot_be_rebound_to_a_new_credential_by_bulk_update(vpn_range):
    from django.db import IntegrityError, transaction

    from engine.models import VpnSession

    row = profile(vpn_range)
    session = connect(row)
    with pytest.raises(IntegrityError), transaction.atomic():
        VpnSession.objects.filter(pk=session.session_id).update(profile=None)


def test_crl_publication_cannot_be_rolled_back_by_bulk_update(vpn_range):
    from django.db import IntegrityError, transaction

    from engine.models import VpnCrlState

    row = VpnCrlState.objects.create(pk=1, requested_revision=2, published_revision=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        VpnCrlState.objects.filter(pk=row.pk).update(published_revision=1)


def test_cleanup_uses_the_exact_issuance_record_after_original_credential_revocation(vpn_range, monkeypatch):
    from engine.models import ProvisionerLaunchIntent
    from engine.services._vpn_profile_operations import authorize_vpn_operation
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    row.state, row.certificate_serial = "ready", "1234"
    row.save(update_fields=["state", "certificate_serial"])
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    reconcile_vpn_profiles(limit=10)
    confirm_issuer_absence(monkeypatch)
    row.refresh_from_db()
    intent = ProvisionerLaunchIntent.objects.get(operation_id=row.provisioner_operation_id)
    authorize_vpn_operation(intent.payload, intent.operation_id)
    ProvisionerLaunchIntent.objects.filter(operation_id=row.issuance_operation_id).delete()
    with pytest.raises(PermissionError, match="VPN operation unavailable"):
        authorize_vpn_operation(intent.payload, intent.operation_id)


def test_credential_lookup_outage_denies_access_without_irreversible_certificate_revocation(vpn_range, monkeypatch):
    from engine.services._vpn_profiles import reconcile_vpn_profiles

    row = profile(vpn_range)

    def unavailable(_credential):
        raise ValueError("Credential lookup temporarily unavailable")

    monkeypatch.setattr("engine.services._vpn_profiles.resolve_current_credential", unavailable)
    assert reconcile_vpn_profiles(limit=10) == 0
    row.refresh_from_db()
    assert row.state == "ready"
    assert row.revoked_at is None


def test_issuance_and_original_authority_are_rolled_back_when_strict_audit_fails(vpn_range, monkeypatch):
    from engine.models import OperationInput, ProvisionerLaunchIntent, VpnAccessProfile
    from engine.services._vpn_profiles import request_vpn_profile

    def audit_unavailable(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("shared.audit.audit_log", audit_unavailable)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    assert not VpnAccessProfile.objects.exists()
    assert not ProvisionerLaunchIntent.objects.exists()
    assert not OperationInput.objects.exists()


@pytest.mark.parametrize("defect", ["wrong_signature", "expired"])
def test_profile_download_rejects_invalid_signed_or_expired_certificate(vpn_range, monkeypatch, defect):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from engine.services import VpnProfileUnavailable, get_openvpn_profile
    from engine.services._vpn_profiles import request_vpn_profile

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    now = timezone.now()
    ca_key, client_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic VPN CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    signer = ec.generate_private_key(ec.SECP256R1()) if defect == "wrong_signature" else ca_key
    expires = now - timedelta(minutes=1) if defect == "expired" else now + timedelta(minutes=30)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"access-{row.pk}")]))
        .issuer_name(name)
        .public_key(client_key.public_key())
        .serial_number(1234)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(expires)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(signer, hashes.SHA256())
    )
    row.state, row.certificate_serial = "ready", "1234"
    row.certificate_fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    row.issuer_fingerprint, row.secret_ref = ca.fingerprint(hashes.SHA256()).hex(), "synthetic-profile-reference"
    row.save(
        update_fields=["state", "certificate_serial", "certificate_fingerprint", "issuer_fingerprint", "secret_ref"]
    )
    material = "client\ndev tun\nproto udp\nremote 203.0.113.7 1194\nnobind\nremote-cert-tls server\n"
    material += "verify-x509-name shifter-openvpn-server name\nauth-nocache\n"
    blocks = {
        "cert": cert.public_bytes(serialization.Encoding.PEM),
        "ca": ca.public_bytes(serialization.Encoding.PEM),
        "key": client_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        "tls-crypt": b"SYNTHETIC\n",
    }
    material += "".join(f"<{name}>\n{value.decode()}</{name}>\n" for name, value in blocks.items())
    monkeypatch.setattr("engine.services._vpn.get_openvpn_profile_secret", lambda _reference: material)
    with pytest.raises(VpnProfileUnavailable):
        get_openvpn_profile(vpn_range.credential, vpn_range.request.request_id)


def test_retired_profile_reissuance_keeps_old_identity_revoked(vpn_range):
    from engine.models import VpnAccessProfile
    from engine.services._vpn_profiles import request_vpn_profile

    old = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    old.state, old.revoked_at = "revoked", timezone.now()
    old.save(update_fields=["state", "revoked_at"])
    replacement = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    assert replacement.pk != old.pk
    assert replacement.credential_uuid == old.credential_uuid
    assert replacement.issuance_operation_id != old.issuance_operation_id
    assert VpnAccessProfile.objects.count() == 2
    old.refresh_from_db()
    assert old.state == "revoked"


def test_revoked_pending_issuance_waits_for_native_worker_absence_before_cleanup(vpn_range, monkeypatch):
    from engine.launch_interrupt import drain_due_interrupts
    from engine.models import InterruptState, ProvisionerLaunchIntent
    from engine.services._vpn_profiles import reconcile_vpn_profiles, request_vpn_profile
    from management.models import ServiceCredentialAdmission
    from shared.cloud.types import TaskInterruptDisposition

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    origin = ProvisionerLaunchIntent.objects.get(operation_id=row.issuance_operation_id)
    ServiceCredentialAdmission.objects.filter(uuid=row.credential_uuid).update(is_active=False)
    reconcile_vpn_profiles(limit=10)
    origin.refresh_from_db()
    assert origin.interrupt_state == InterruptState.REQUESTED
    observed = []

    from types import SimpleNamespace

    def stopping(namespace, task_ref, expected_identity):
        observed.append((task_ref, expected_identity["command"], expected_identity["task_identity"]))
        return TaskInterruptDisposition.STOPPING

    monkeypatch.setattr("engine.ecs.get_task_runner", lambda: SimpleNamespace(interrupt_task=stopping))
    assert drain_due_interrupts(1) == 1
    row.refresh_from_db()
    assert row.provisioner_operation == "issue"
    ProvisionerLaunchIntent.objects.filter(pk=origin.pk).update(interrupt_next_attempt_at=timezone.now())
    monkeypatch.setattr(
        "engine.ecs.get_task_runner",
        lambda: SimpleNamespace(interrupt_task=lambda *_args: TaskInterruptDisposition.TERMINAL_ABSENT),
    )
    assert drain_due_interrupts(1) == 1
    row.refresh_from_db()
    origin.refresh_from_db()
    assert row.provisioner_operation == "revoke"
    assert origin.interrupt_state == InterruptState.DESTROY_ENQUEUED
    assert observed[0][0] == origin.task_ref
    assert observed[0][2] == str(origin.intent_id)


def test_failed_native_issuance_retires_profile_and_keeps_cleanup_evidence(vpn_range):
    from engine.models import OperationResultInbox
    from engine.services import apply_pending_operation_results
    from engine.services._vpn_profiles import request_vpn_profile
    from shared.operation_envelope import build_operation_envelope, canonical_payload_digest

    row = request_vpn_profile(vpn_range.credential, vpn_range.request.request_id)
    payload = {"reason_code": "cloud_operation_failed", "diagnostic": "VPN operation failed"}
    envelope = build_operation_envelope(
        operation_id=row.provisioner_operation_id,
        request_id=row.generation,
        resource="vpn-profile",
        operation="issue",
        payload=payload,
    )
    result = OperationResultInbox.objects.create(
        operation_id=row.provisioner_operation_id,
        request_id=row.generation,
        resource="vpn-profile",
        operation="issue",
        contract_version=envelope["contract_version"],
        result_kind="TERMINAL_FAILURE",
        result_step="vpn_pki_failed",
        result_identity=str(uuid4()),
        payload_digest=canonical_payload_digest(payload),
        envelope=envelope,
    )
    assert apply_pending_operation_results() == 1
    result.refresh_from_db()
    row.refresh_from_db()
    assert result.disposition == "APPLIED"
    assert row.state == "revoked"
    assert row.revoked_at is not None
    assert row.issuance_operation_id is not None
