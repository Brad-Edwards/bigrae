"""Apply public certificate evidence and signed CRLs in the native result inbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.types import CertificateIssuerPublicKeyTypes
from django.utils import timezone

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.operation_authority import credential_reference_from_record

if TYPE_CHECKING:
    from typing import Any

    from engine.models import OperationResultInbox, VpnAccessProfile, VpnCrlState


def _bind_certificate(profile: VpnAccessProfile, receipt: dict[str, Any], operation: str) -> None:
    """Validate the public certificate receipt against the admitted native profile."""
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


def apply_vpn_pki_result(result: OperationResultInbox, receipt: dict[str, Any]) -> tuple[str, str]:
    """Apply authenticated PKI evidence for the exact current profile operation."""

    admission = _sibling_disposition(result)
    if admission is not None:
        return admission
    if result.result_kind == "TERMINAL_FAILURE":
        return _apply_failure(result, receipt)
    return _apply_success(result, receipt)


def _apply_failure(result: OperationResultInbox, receipt: dict[str, Any]) -> tuple[str, str]:
    """Retain PKI failure evidence without discarding required native cleanup."""
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


def _sibling_disposition(result: OperationResultInbox) -> tuple[str, str] | None:
    """Defer ordered siblings and reject conflicting native result receipts."""
    from engine.models import OperationResultDisposition

    from ._operation_apply_admission import has_conflicting_sibling, has_earlier_pending_sibling

    if has_earlier_pending_sibling(result):
        return "", ""
    if has_conflicting_sibling(result):
        return OperationResultDisposition.REJECTED_CONFLICT, "VPN PKI receipt conflict"
    return None


def _apply_success(result: OperationResultInbox, receipt: dict[str, Any]) -> tuple[str, str]:
    """Commit verified certificate evidence and CRL publication in the native result transaction."""
    from engine.models import OperationInput, OperationResultDisposition, ProvisionerLaunchIntent, VpnAccessProfile

    from ._vpn_profile_operations import validate_vpn_operation

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
        crl, expected_serials = _verified_crl(receipt, value, profile, result.operation)
        state = _publication_state(receipt)
        _apply_profile_evidence(profile, receipt, result)
        _publish_crl(state, receipt, crl, expected_serials)
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


def _verified_crl(
    receipt: dict[str, Any], value: dict[str, Any], profile: VpnAccessProfile, operation: str
) -> tuple[x509.CertificateRevocationList, set[int]]:
    """Verify the configured issuer signature, CRL bounds and exact admitted revocations."""
    ca = x509.load_pem_x509_certificate(receipt["issuer_certificate"].encode())
    crl = x509.load_pem_x509_crl(receipt["crl"].encode())
    expected_serials = _expected_revocations(value, receipt, profile, operation, crl)
    if (
        ca.fingerprint(hashes.SHA256()).hex() != receipt["issuer_fingerprint"]
        or not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        or not crl.is_signature_valid(cast(CertificateIssuerPublicKeyTypes, ca.public_key()))
        or crl.issuer != ca.subject
        or crl.next_update_utc is None
        or crl.next_update_utc <= timezone.now()
        or crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number != value["crl_revision"]
        or {entry.serial_number for entry in crl} != expected_serials
    ):
        raise ValueError("VPN signed CRL unavailable")
    return crl, expected_serials


def _expected_revocations(
    value: dict[str, Any],
    receipt: dict[str, Any],
    profile: VpnAccessProfile,
    operation: str,
    crl: x509.CertificateRevocationList,
) -> set[int]:
    """Include a recovered uncertain certificate only while its admitted lifetime remains live."""
    expected_serials = {int(entry["serial"]) for entry in value["revocations"]}
    if (
        operation == "revoke"
        and not value["certificate_serial"]
        and receipt["certificate_serial"]
        and profile.expires_at > crl.last_update_utc
    ):
        expected_serials.add(int(receipt["certificate_serial"]))
    return expected_serials


def _apply_profile_evidence(profile: VpnAccessProfile, receipt: dict[str, Any], result: OperationResultInbox) -> None:
    """Retain certificate, public revocation and private-material removal evidence separately."""
    _bind_certificate(profile, receipt, result.operation)
    if result.operation == "revoke" and profile.revocation_operation_id is None:
        profile.revocation_operation_id = result.operation_id
        profile.save(update_fields=["revocation_operation_id", "updated_at"])
    if result.operation == "purge" and profile.secret_removed_at is None:
        profile.secret_removed_at = timezone.now()
        profile.save(update_fields=["secret_removed_at", "updated_at"])


def _publish_crl(
    state: VpnCrlState, receipt: dict[str, Any], crl: x509.CertificateRevocationList, expected_serials: set[int]
) -> None:
    """Publish a newer verified CRL only when it covers every current revocation."""
    from engine.models import VpnAccessProfile

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


def _publication_state(receipt: dict[str, Any]) -> VpnCrlState:
    """Lock and check the current issuer before mutating certificate evidence."""
    from engine.models import VpnCrlState

    state = VpnCrlState.objects.select_for_update().get(pk=1)
    if state.issuer_fingerprint and state.issuer_fingerprint != receipt["issuer_fingerprint"]:
        raise ValueError("VPN issuer changed")
    return state
