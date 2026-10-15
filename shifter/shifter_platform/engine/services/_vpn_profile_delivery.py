"""Validate the original credential and actual individual key before download."""

import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.utils import timezone

from shared.remote_access import OPENVPN_PROFILE_VERSION, OpenVpnProfile, validate_openvpn_profile_for_endpoint


def get_individual_vpn_profile(credential, request_id, *, participation: bool = False):
    from ._vpn import VpnProfilePending, VpnProfileUnavailable, get_openvpn_profile_secret
    from ._vpn_profiles import authorize_vpn_profile, request_vpn_profile

    try:
        row = request_vpn_profile(credential, request_id, participation=participation)
        if row.state == "pending":
            raise VpnProfilePending("VPN profile preparing")
        authorize_vpn_profile(row)
        material = get_openvpn_profile_secret(row.secret_ref)
        endpoint = row.range.vpn_access_binding
        content = validate_openvpn_profile_for_endpoint(material, endpoint["endpoint"], endpoint["port"])
        blocks = {
            name: re.search(rf"<{name}>\n(.*?)</{name}>", material, re.S).group(1).encode()
            for name in ("cert", "key", "ca")
        }
        certificate = x509.load_pem_x509_certificate(blocks["cert"])
        ca = x509.load_pem_x509_certificate(blocks["ca"])
        key = serialization.load_pem_private_key(blocks["key"], password=None)
        certificate.verify_directly_issued_by(ca)
        now = timezone.now()
        if (
            certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != f"access-{row.pk}"
            or str(certificate.serial_number) != row.certificate_serial
            or certificate.fingerprint(hashes.SHA256()).hex() != row.certificate_fingerprint
            or ca.fingerprint(hashes.SHA256()).hex() != row.issuer_fingerprint
            or certificate.not_valid_before_utc > now
            or certificate.not_valid_after_utc <= now
            or certificate.not_valid_after_utc > row.expires_at
            or ca.not_valid_before_utc > now
            or ca.not_valid_after_utc <= now
            or ExtendedKeyUsageOID.CLIENT_AUTH
            not in certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            or key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            != certificate.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ):
            raise ValueError("VPN certificate changed")
        # Provider I/O cannot carry a stale grant across revocation or generation change.
        row.refresh_from_db()
        authorize_vpn_profile(row)
        return OpenVpnProfile(content, row.generation, OPENVPN_PROFILE_VERSION)
    except VpnProfilePending:
        raise
    except Exception:
        raise VpnProfileUnavailable("VPN profile unavailable") from None
