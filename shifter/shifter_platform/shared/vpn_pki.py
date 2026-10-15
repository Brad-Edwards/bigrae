"""Public VPN PKI fields transported through the existing operation envelope."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _bounded_certificate_serial(value):
    if value and int(value) >= 2**159:
        raise ValueError("Invalid certificate serial")
    return value


class RevokedCertificate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    serial: str = Field(pattern=r"^[1-9][0-9]{0,48}$")
    revoked_at: datetime

    @field_validator("serial")
    @classmethod
    def bounded_serial(cls, value):
        return _bounded_certificate_serial(value)

    @field_validator("revoked_at")
    @classmethod
    def aware_revocation_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Invalid PKI time")
        return value


class VpnPkiInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: UUID
    generation: UUID
    expires_at: datetime
    crl_issued_at: datetime
    crl_revision: int = Field(strict=True, ge=1, lt=2**63)
    revocations: list[RevokedCertificate] = Field(max_length=4096)
    issuer_fingerprint: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    certificate_serial: str = Field(pattern=r"^(?:[1-9][0-9]{0,48})?$")
    secret_ref: str = Field(max_length=512)
    storage_project_id: str = Field(default="", pattern=r"^[a-z0-9-]{0,128}$")
    storage_environment: str = Field(default="", pattern=r"^[a-z0-9-]{0,64}$")
    issuer_secret_ref: str = Field(default="", max_length=512)

    @field_validator("certificate_serial")
    @classmethod
    def bounded_serial(cls, value):
        return _bounded_certificate_serial(value)

    @field_validator("expires_at", "crl_issued_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("Invalid PKI time")
        return value


class VpnPkiReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: UUID
    issuer_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    issuer_certificate: str = Field(max_length=16384)
    certificate_serial: str = Field(pattern=r"^(?:[1-9][0-9]{0,48})?$")
    certificate_fingerprint: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    secret_ref: str = Field(max_length=512)
    crl_revision: int = Field(strict=True, ge=1, lt=2**63)
    crl: str = Field(max_length=262144)

    @field_validator("certificate_serial")
    @classmethod
    def bounded_serial(cls, value):
        return _bounded_certificate_serial(value)

    @field_validator("issuer_certificate", "crl")
    @classmethod
    def public_pem(cls, value):
        if "PRIVATE KEY" in value:
            raise ValueError("Invalid public PKI receipt")
        return value


def parse_vpn_pki_input(value):
    try:
        return VpnPkiInput.model_validate(value)
    except Exception:
        raise ValueError("VPN operation input unavailable") from None


def parse_vpn_pki_receipt(value):
    try:
        return VpnPkiReceipt.model_validate(value).model_dump(mode="json")
    except Exception:
        raise ValueError("VPN operation receipt unavailable") from None
