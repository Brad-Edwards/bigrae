"""Validate the original credential and actual individual key before download."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.utils import timezone

from shared.remote_access import OPENVPN_PROFILE_VERSION, OpenVpnProfile, validate_openvpn_profile_for_endpoint

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from engine.models import VpnAccessProfile
    from shared.credentials import CredentialContext


def get_individual_vpn_profile(
    credential: CredentialContext, request_id: UUID, *, participation: bool = False
) -> OpenVpnProfile:
    """Reauthorize the original profile and verify its delivered certificate and key."""
    from ._vpn import VpnProfilePending, VpnProfileUnavailable, get_openvpn_profile_secret
    from ._vpn_profiles import authorize_vpn_profile, request_vpn_profile

    try:
        row = request_vpn_profile(credential, request_id, participation=participation)
        if row.state == "pending":
            raise VpnProfilePending("VPN profile preparing")
        authorize_vpn_profile(row)
        material = get_openvpn_profile_secret(row.secret_ref)
        endpoint = row.range.vpn_access_binding
        if not isinstance(endpoint, dict):
            raise ValueError("VPN endpoint unavailable")
        content = validate_openvpn_profile_for_endpoint(material, endpoint["endpoint"], endpoint["port"])
        _verify_profile_material(row, material)
        # Provider I/O cannot carry a stale grant across revocation or generation change.
        row.refresh_from_db()
        authorize_vpn_profile(row)
        return OpenVpnProfile(content, row.generation, OPENVPN_PROFILE_VERSION)
    except VpnProfilePending:
        raise
    except Exception:
        raise VpnProfileUnavailable("VPN profile unavailable") from None


def _verify_profile_material(row: VpnAccessProfile, material: str) -> None:
    """Verify the delivered key, leaf and issuer against retained certificate evidence."""
    blocks = {}
    for name in ("cert", "key", "ca"):
        match = re.search(rf"<{name}>\n(.*?)</{name}>", material, re.S)
        if match is None:
            raise ValueError("VPN profile material unavailable")
        blocks[name] = match.group(1).encode()
    certificate = x509.load_pem_x509_certificate(blocks["cert"])
    ca = x509.load_pem_x509_certificate(blocks["ca"])
    key = serialization.load_pem_private_key(blocks["key"], password=None)
    certificate.verify_directly_issued_by(ca)
    _verify_certificate_lifetimes(certificate, ca, row.expires_at)
    if (
        certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != f"access-{row.pk}"
        or str(certificate.serial_number) != row.certificate_serial
        or certificate.fingerprint(hashes.SHA256()).hex() != row.certificate_fingerprint
        or ca.fingerprint(hashes.SHA256()).hex() != row.issuer_fingerprint
        or ExtendedKeyUsageOID.CLIENT_AUTH
        not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        or key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        != certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ):
        raise ValueError("VPN certificate changed")


def _verify_certificate_lifetimes(leaf: x509.Certificate, issuer: x509.Certificate, expires_at: datetime) -> None:
    """Require both certificates to be live and the leaf to stay inside its admitted expiry."""
    now = timezone.now()
    if (
        leaf.not_valid_before_utc > now
        or leaf.not_valid_after_utc <= now
        or leaf.not_valid_after_utc > expires_at
        or issuer.not_valid_before_utc > now
        or issuer.not_valid_after_utc <= now
    ):
        raise ValueError("VPN certificate changed")
