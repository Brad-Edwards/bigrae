"""Public PKI metadata rejects ambiguous times and invalid X.509 serials."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.vpn_pki import RevokedCertificate, VpnPkiInput, VpnPkiReceipt


def test_revocation_time_requires_an_explicit_timezone():
    with pytest.raises(ValidationError):
        RevokedCertificate(serial="123", revoked_at=datetime(2026, 10, 14))
    assert RevokedCertificate(serial="123", revoked_at=datetime(2026, 10, 14, tzinfo=UTC)).serial == "123"


@pytest.mark.parametrize("contract", [VpnPkiInput, VpnPkiReceipt])
def test_operation_and_receipt_reject_out_of_range_certificate_serial(contract):
    fields = {
        "profile_id": uuid4(),
        "certificate_serial": str(2**159),
        "issuer_fingerprint": "a" * 64,
        "secret_ref": "synthetic-profile",
        "crl_revision": 1,
    }
    if contract is VpnPkiInput:
        fields.update(
            generation=uuid4(),
            expires_at=datetime.now(UTC),
            crl_issued_at=datetime.now(UTC),
            revocations=[],
        )
    else:
        fields.update(
            issuer_certificate="synthetic-public-certificate",
            certificate_fingerprint="b" * 64,
            crl="synthetic-public-crl",
        )
    with pytest.raises(ValidationError):
        contract.model_validate(fields)
