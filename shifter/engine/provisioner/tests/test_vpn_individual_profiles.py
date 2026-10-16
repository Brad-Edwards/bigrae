"""Individual OpenVPN identities and CA-signed revocation lists."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import vpn_access
from tests.conftest import build_vpn_issuer_payload


def test_two_access_profiles_for_one_generation_have_independent_keys_and_identities():
    issuer = vpn_access.load_issuer(build_vpn_issuer_payload())
    generation = uuid4()
    certificates = [
        vpn_access.mint_individual_profile(
            issuer,
            generation=generation,
            profile_id=uuid4(),
            not_after=datetime.now(UTC) + timedelta(hours=1),
            endpoint=vpn_access.OpenVpnPoolEndpoint("203.0.113.7", 1194),
        )
        for _ in range(2)
    ]
    first, second = certificates
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert first[2] != second[2]
    for profile, _serial, fingerprint in certificates:
        cert = x509.load_pem_x509_certificate(profile.split("<cert>\n")[1].split("</cert>")[0].encode())
        assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value.startswith("access-")
        assert len(fingerprint) == 64
        assert "ca_private_key" not in profile
        issuer.ca_certificate.public_key().verify(
            cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm)
        )


def test_revocation_list_is_signed_by_existing_ca_and_contains_only_retired_serials():
    issuer = vpn_access.load_issuer(build_vpn_issuer_payload())
    now = datetime.now(UTC)
    pem = vpn_access.build_signed_crl(issuer, [(1234, now)], now=now)
    crl = x509.load_pem_x509_crl(pem.encode())
    assert [entry.serial_number for entry in crl] == [1234]
    assert crl.next_update_utc > now
    issuer.ca_certificate.public_key().verify(
        crl.signature, crl.tbs_certlist_bytes, ec.ECDSA(crl.signature_hash_algorithm)
    )


def test_individual_profile_cannot_outlive_the_ca():
    issuer = vpn_access.load_issuer(build_vpn_issuer_payload(valid_days=1))
    with pytest.raises(ValueError):
        vpn_access.mint_individual_profile(
            issuer,
            generation=uuid4(),
            profile_id=uuid4(),
            not_after=datetime.now(UTC) + timedelta(days=2),
            endpoint=vpn_access.OpenVpnPoolEndpoint("203.0.113.7", 1194),
        )


def test_native_crl_store_rejects_wrong_signer_expiry_and_rollback(tmp_path):
    from shifter_openvpn.crl import CrlStore

    issuer = vpn_access.load_issuer(build_vpn_issuer_payload())
    other = vpn_access.load_issuer(build_vpn_issuer_payload())
    now = datetime.now(UTC)
    ca = tmp_path / "ca.crt"
    ca.write_text(issuer.ca_pem)
    path = tmp_path / "crl.pem"
    store = CrlStore(str(ca), str(path))
    latest = vpn_access.build_signed_crl(issuer, [(1234, now)], now=now, revision=2)
    store.install(latest)
    assert store.valid()
    with pytest.raises(ValueError):
        store.install(vpn_access.build_signed_crl(other, [], now=now, revision=3))
    with pytest.raises(ValueError):
        store.install(vpn_access.build_signed_crl(issuer, [], now=now, revision=1))
    with pytest.raises(ValueError):
        store.install(vpn_access.build_signed_crl(issuer, [], now=now - timedelta(days=2), revision=3))
    assert path.read_text() == latest
    path.unlink()
    assert not store.valid()


def test_provisioner_retries_reuse_exact_profile_and_never_return_private_material():
    from vpn_profile_ops import execute_vpn_profile_operation

    class Secrets:
        def __init__(self):
            self.issuer = build_vpn_issuer_payload()
            self.profile = None

        def read_issuer(self):
            return self.issuer

        def get_or_create_individual_profile(self, profile_id, factory):
            if self.profile is None:
                self.profile = factory()
            return f"projects/synthetic-project/secrets/profile-{profile_id}", self.profile

    ops = Secrets()
    payload = {
        "profile_id": str(uuid4()),
        "generation": str(uuid4()),
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "crl_issued_at": datetime.now(UTC).isoformat(),
        "crl_revision": 1,
        "revocations": [],
        "issuer_fingerprint": "",
        "certificate_serial": "",
        "secret_ref": "",
    }
    endpoint = vpn_access.OpenVpnPoolEndpoint("203.0.113.7", 1194)
    first = execute_vpn_profile_operation(payload, "issue", ops, endpoint)
    second = execute_vpn_profile_operation(payload, "issue", ops, endpoint)
    assert first == second
    from shared.operation_results import parse_result_payload

    assert parse_result_payload("vpn-profile", "issue", step="vpn_pki_ready", payload=first) == first
    assert first["certificate_serial"] == second["certificate_serial"]
    assert first["certificate_fingerprint"] == second["certificate_fingerprint"]
    assert first["secret_ref"] == second["secret_ref"]
    assert "PRIVATE KEY" not in str(first)
    assert "ca_private_key" not in str(first)
    assert (
        x509.load_pem_x509_crl(first["crl"].encode())
        .extensions.get_extension_for_class(x509.CRLNumber)
        .value.crl_number
        == 1
    )


def test_expired_profile_secret_is_removed_without_adding_expired_serial_to_crl():
    from vpn_profile_ops import execute_vpn_profile_operation

    class Secrets:
        def read_issuer(self):
            return issuer_payload

        def delete_individual_profile(self, profile_id):
            deleted.append(profile_id)

    issuer_payload = build_vpn_issuer_payload()
    deleted = []
    profile_id = uuid4()
    now = datetime.now(UTC)
    receipt = execute_vpn_profile_operation(
        {
            "profile_id": str(profile_id),
            "generation": str(uuid4()),
            "expires_at": (now - timedelta(minutes=1)).isoformat(),
            "crl_issued_at": now.isoformat(),
            "crl_revision": 2,
            "revocations": [],
            "issuer_fingerprint": "",
            "certificate_serial": "1234",
            "secret_ref": "",
        },
        "purge",
        Secrets(),
        None,
    )
    assert deleted == [profile_id]
    assert list(x509.load_pem_x509_crl(receipt["crl"].encode())) == []


def test_cleanup_recovers_uncertain_certificate_from_existing_secret_and_revokes_it():
    from vpn_profile_ops import execute_vpn_profile_operation

    issuer_payload = build_vpn_issuer_payload()
    issuer = vpn_access.load_issuer(issuer_payload)
    now = datetime.now(UTC)
    profile_id = uuid4()
    material, serial, fingerprint = vpn_access.mint_individual_profile(
        issuer,
        generation=uuid4(),
        profile_id=profile_id,
        not_after=now + timedelta(hours=1),
        endpoint=vpn_access.OpenVpnPoolEndpoint("203.0.113.7", 1194),
    )
    deleted = []

    class Secrets:
        def read_issuer(self):
            return issuer_payload

        def read_individual_profile(self, identifier):
            assert identifier == profile_id
            return "projects/synthetic-project/secrets/profile", material

        def delete_individual_profile(self, identifier):
            deleted.append(identifier)

    payload = {
        "profile_id": str(profile_id),
        "generation": str(uuid4()),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "crl_issued_at": now.isoformat(),
        "crl_revision": 2,
        "revocations": [],
        "issuer_fingerprint": "",
        "certificate_serial": "",
        "secret_ref": "",
    }
    ops = Secrets()
    receipt = execute_vpn_profile_operation(payload, "revoke", ops, None)
    assert receipt["certificate_serial"] == serial
    assert receipt["certificate_fingerprint"] == fingerprint
    assert [entry.serial_number for entry in x509.load_pem_x509_crl(receipt["crl"].encode())] == [int(serial)]
    # Private material stays available until the public revocation evidence is
    # durably accepted. Retrying before that point must return the same receipt.
    assert deleted == []
    assert receipt == execute_vpn_profile_operation(payload, "revoke", ops, None)


def test_failed_issuance_reports_only_authored_failure_and_no_private_diagnostic(monkeypatch):
    from types import SimpleNamespace

    import vpn_profile_ops

    request_id, profile_id, operation_id = str(uuid4()), str(uuid4()), str(uuid4())
    monkeypatch.setattr(
        "provisioner_db_operation_input.get_operation_input",
        lambda **_kwargs: SimpleNamespace(
            request_id=request_id,
            operation_id=operation_id,
            payload={
                "profile_id": profile_id,
                "generation": request_id,
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "crl_issued_at": datetime.now(UTC).isoformat(),
                "crl_revision": 1,
                "revocations": [],
                "issuer_fingerprint": "",
                "certificate_serial": "",
                "secret_ref": "",
                "storage_project_id": "synthetic-secrets",
                "storage_environment": "synthetic",
                "issuer_secret_ref": "projects/synthetic-platform/secrets/issuer",
            },
        ),
    )

    class Secrets:
        def __init__(self, **_kwargs):
            pass

        def read_issuer(self):
            observed.append("issuer-read")
            raise ValueError("PRIVATE_KEY_DIAGNOSTIC_2319")

    observed = []
    monkeypatch.setattr("vpn_secrets.get_vpn_secret_ops", Secrets)
    monkeypatch.setattr(vpn_profile_ops, "openvpn_pool_endpoint", lambda: None)

    recorded = []
    monkeypatch.setattr(
        "provisioner_db_appends.append_operation_step_result", lambda *args, **kwargs: recorded.append(kwargs)
    )
    with pytest.raises(ValueError, match="VPN operation failed"):
        vpn_profile_ops.run_vpn_profile_operation(request_id, profile_id, "issue", operation_id)
    assert recorded[0]["step"] == "vpn_pki_failed"
    assert "PRIVATE_KEY_DIAGNOSTIC_2319" not in str(recorded)
    assert observed == ["issuer-read"]
    from shared.operation_results import parse_result_payload

    assert parse_result_payload("vpn-profile", "issue", step="vpn_pki_failed", payload=recorded[0]["result_payload"])
