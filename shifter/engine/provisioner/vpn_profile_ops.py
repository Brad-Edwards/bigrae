"""Native individual profile issuance and signed CRLs at the existing CA owner."""

from datetime import UTC, datetime
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from shared.vpn_pki import parse_vpn_pki_input, parse_vpn_pki_receipt

from vpn_access import build_signed_crl, load_issuer, mint_individual_profile, openvpn_pool_endpoint


def execute_vpn_profile_operation(payload, operation, secret_ops, endpoint):
    """Return a public, deterministic receipt; private keys stay in provider secrets."""
    value = parse_vpn_pki_input(payload)
    if operation not in {"issue", "revoke", "refresh-crl", "purge"}:
        raise ValueError("VPN operation unavailable")
    issuer = load_issuer(secret_ops.read_issuer())
    issuer_fingerprint = issuer.ca_certificate.fingerprint(hashes.SHA256()).hex()
    if value.issuer_fingerprint and value.issuer_fingerprint != issuer_fingerprint:
        raise ValueError("VPN issuer changed")
    serial, fingerprint, secret_ref = "", "", ""
    revocations = [(int(item.serial), item.revoked_at) for item in value.revocations]
    if operation == "issue":
        if endpoint is None:
            raise ValueError("VPN endpoint unavailable")
        secret_ref, material = secret_ops.get_or_create_individual_profile(
            value.profile_id,
            lambda: mint_individual_profile(
                issuer,
                generation=value.generation,
                profile_id=value.profile_id,
                not_after=value.expires_at,
                endpoint=endpoint,
            )[0],
        )
        cert = x509.load_pem_x509_certificate(material.split("<cert>\n", 1)[1].split("</cert>", 1)[0].encode())
        if (
            cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != f"access-{value.profile_id}"
            or cert.issuer != issuer.ca_certificate.subject
            or cert.not_valid_after_utc <= datetime.now(UTC)
            or abs((cert.not_valid_after_utc - value.expires_at).total_seconds()) >= 1
        ):
            raise ValueError("VPN stored certificate unavailable")
        serial, fingerprint = str(cert.serial_number), cert.fingerprint(hashes.SHA256()).hex()
    elif operation == "revoke":
        if value.certificate_serial:
            if value.expires_at > value.crl_issued_at and value.certificate_serial not in {
                item.serial for item in value.revocations
            }:
                raise ValueError("VPN certificate revocation unavailable")
        else:
            existing = secret_ops.read_individual_profile(value.profile_id)
            if existing is not None:
                secret_ref, material = existing
                cert = x509.load_pem_x509_certificate(material.split("<cert>\n", 1)[1].split("</cert>", 1)[0].encode())
                cert.verify_directly_issued_by(issuer.ca_certificate)
                if (
                    cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != f"access-{value.profile_id}"
                    or abs((cert.not_valid_after_utc - value.expires_at).total_seconds()) >= 1
                ):
                    raise ValueError("VPN uncertain certificate unavailable")
                serial, fingerprint = str(cert.serial_number), cert.fingerprint(hashes.SHA256()).hex()
                if cert.not_valid_after_utc > value.crl_issued_at:
                    revocations.append((cert.serial_number, value.crl_issued_at))
    elif operation == "purge":
        secret_ops.delete_individual_profile(value.profile_id)
    crl = build_signed_crl(
        issuer,
        revocations,
        now=value.crl_issued_at,
        revision=value.crl_revision,
    )
    return parse_vpn_pki_receipt(
        {
            "profile_id": str(value.profile_id),
            "issuer_fingerprint": issuer_fingerprint,
            "issuer_certificate": issuer.ca_pem,
            "certificate_serial": serial,
            "certificate_fingerprint": fingerprint,
            "secret_ref": secret_ref,
            "crl_revision": value.crl_revision,
            "crl": crl,
        }
    )


def run_vpn_profile_operation(request_id, profile_id, operation, operation_id):
    from provisioner_db_appends import OperationRef, append_operation_step_result
    from provisioner_db_operation_input import get_operation_input
    from vpn_secrets import get_vpn_secret_ops

    validated = get_operation_input(
        operation_id=operation_id, request_id=request_id, resource="vpn-profile", operation=operation
    )
    if validated.payload["profile_id"] != str(UUID(profile_id)) or validated.payload["generation"] != str(
        UUID(request_id)
    ):
        raise ValueError("VPN operation identity unavailable")
    try:
        value = parse_vpn_pki_input(validated.payload)
        if not all((value.storage_project_id, value.storage_environment, value.issuer_secret_ref)):
            raise ValueError("VPN storage binding unavailable")
        secret_ops = get_vpn_secret_ops(
            storage_project_id=value.storage_project_id,
            storage_environment=value.storage_environment,
            issuer_secret=value.issuer_secret_ref,
        )
        receipt = execute_vpn_profile_operation(validated.payload, operation, secret_ops, openvpn_pool_endpoint())
    except Exception:
        append_operation_step_result(
            OperationRef(validated.request_id, validated.operation_id),
            resource="vpn-profile",
            operation=operation,
            step="vpn_pki_failed",
            result_payload={"reason_code": "cloud_operation_failed", "diagnostic": "VPN operation failed"},
        )
        raise ValueError("VPN operation failed") from None
    append_operation_step_result(
        OperationRef(validated.request_id, validated.operation_id),
        resource="vpn-profile",
        operation=operation,
        step="vpn_pki_ready",
        result_payload=receipt,
    )
