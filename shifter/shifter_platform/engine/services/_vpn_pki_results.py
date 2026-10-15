"""Apply public certificate evidence and signed CRLs in the native result inbox."""

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from django.utils import timezone

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.operation_authority import credential_reference_from_record


def _bind_certificate(profile, receipt, operation):
    if operation == "issue" or (operation == "revoke" and receipt["certificate_serial"]):
        if not all(receipt[key] for key in ("certificate_serial", "certificate_fingerprint", "secret_ref")):
            raise ValueError("VPN certificate receipt unavailable")
        for name in ("certificate_serial", "certificate_fingerprint", "issuer_fingerprint", "secret_ref"):
            if getattr(profile, name) and getattr(profile, name) != receipt[name]:
                raise ValueError("VPN certificate receipt changed")
            setattr(profile, name, receipt[name])
        if operation == "issue" and profile.revoked_at is None:
            profile.state = "ready"
        profile.save(
            update_fields=[
                "state",
                "certificate_serial",
                "certificate_fingerprint",
                "issuer_fingerprint",
                "secret_ref",
                "updated_at",
            ]
        )


def apply_vpn_pki_result(result, receipt):
    from engine.models import (
        OperationInput,
        OperationResultDisposition,
        ProvisionerLaunchIntent,
        VpnAccessProfile,
        VpnCrlState,
    )

    from ._operation_apply_admission import has_conflicting_sibling, has_earlier_pending_sibling
    from ._vpn_profile_operations import validate_vpn_operation

    if has_earlier_pending_sibling(result):
        return "", ""
    if has_conflicting_sibling(result):
        return OperationResultDisposition.REJECTED_CONFLICT, "VPN PKI receipt conflict"
    if result.result_kind == "TERMINAL_FAILURE":
        return _apply_failure(result, receipt)
    profile = VpnAccessProfile.objects.select_for_update().get(provisioner_operation_id=result.operation_id)
    intent = ProvisionerLaunchIntent.objects.get(operation_id=result.operation_id)
    value = OperationInput.objects.get(operation_id=result.operation_id).envelope["payload"]
    try:
        validate_vpn_operation(intent.payload, result.operation_id, receipt=True)
        if (
            receipt["profile_id"] != str(profile.pk)
            or receipt["crl_revision"] != value["crl_revision"]
            or result.operation != intent.payload["operation"]
        ):
            raise ValueError("VPN receipt identity changed")
        ca = x509.load_pem_x509_certificate(receipt["issuer_certificate"].encode())
        crl = x509.load_pem_x509_crl(receipt["crl"].encode())
        expected_serials = {int(entry["serial"]) for entry in value["revocations"]}
        if (
            result.operation == "revoke"
            and not value["certificate_serial"]
            and receipt["certificate_serial"]
            and profile.expires_at > crl.last_update_utc
        ):
            expected_serials.add(int(receipt["certificate_serial"]))
        if (
            ca.fingerprint(hashes.SHA256()).hex() != receipt["issuer_fingerprint"]
            or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or not crl.is_signature_valid(ca.public_key())
            or crl.issuer != ca.subject
            or crl.next_update_utc is None
            or crl.next_update_utc <= timezone.now()
            or crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number != value["crl_revision"]
            or {entry.serial_number for entry in crl} != expected_serials
        ):
            raise ValueError("VPN signed CRL unavailable")
        state = VpnCrlState.objects.select_for_update().get(pk=1)
        if state.issuer_fingerprint and state.issuer_fingerprint != receipt["issuer_fingerprint"]:
            raise ValueError("VPN issuer changed")
        _bind_certificate(profile, receipt, result.operation)
        if result.operation == "revoke" and profile.revocation_operation_id is None:
            profile.revocation_operation_id = result.operation_id
            profile.save(update_fields=["revocation_operation_id", "updated_at"])
        if result.operation == "purge" and profile.secret_removed_at is None:
            profile.secret_removed_at = timezone.now()
            profile.save(update_fields=["secret_removed_at", "updated_at"])
        current_revocations = {
            int(serial)
            for serial in VpnAccessProfile.objects.filter(
                revoked_at__isnull=False,
                expires_at__gt=timezone.now(),
                issuer_fingerprint=receipt["issuer_fingerprint"],
            )
            .exclude(certificate_serial="")
            .values_list("certificate_serial", flat=True)
        }
        if receipt["crl_revision"] > state.published_revision and current_revocations <= expected_serials:
            state.published_revision = receipt["crl_revision"]
            state.issuer_fingerprint = receipt["issuer_fingerprint"]
            state.issuer_certificate = receipt["issuer_certificate"]
            state.pem, state.expires_at = receipt["crl"], crl.next_update_utc
            state.save()
    except (ValueError, x509.ExtensionNotFound):
        return OperationResultDisposition.REJECTED_INVALID, "VPN PKI receipt unavailable"
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.RANGE,
            entity_id=profile.range_id,
            action=AuditAction.UPDATE,
            **principal_actor_fields(credential_reference_from_record(profile).principal),
            new_state={
                "profile_uuid": str(profile.pk),
                "operation": result.operation,
                "crl_revision": receipt["crl_revision"],
            },
            context="range_vpn_pki",
            request_id=str(profile.generation),
        ),
        strict=True,
    )
    return OperationResultDisposition.APPLIED, "VPN PKI receipt applied"


def _apply_failure(result, receipt):
    from engine.models import OperationResultDisposition, ProvisionerLaunchIntent, VpnAccessProfile

    from ._vpn_profile_operations import validate_vpn_operation

    profile = VpnAccessProfile.objects.select_for_update().get(provisioner_operation_id=result.operation_id)
    intent = ProvisionerLaunchIntent.objects.get(operation_id=result.operation_id)
    try:
        validate_vpn_operation(intent.payload, result.operation_id, receipt=True)
    except ValueError:
        return OperationResultDisposition.REJECTED_INVALID, "VPN failure receipt unavailable"
    if result.operation == "issue" and profile.revoked_at is None:
        profile.state, profile.revoked_at = "revoked", timezone.now()
        profile.save(update_fields=["state", "revoked_at", "updated_at"])
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.RANGE,
            entity_id=profile.range_id,
            action=AuditAction.UPDATE,
            **principal_actor_fields(credential_reference_from_record(profile).principal),
            new_state={
                "profile_uuid": str(profile.pk),
                "operation": result.operation,
                "outcome": "failed",
                "reason_code": receipt["reason_code"],
            },
            context="range_vpn_pki",
            request_id=str(profile.generation),
        ),
        strict=True,
    )
    return OperationResultDisposition.APPLIED, "VPN operation failed"
