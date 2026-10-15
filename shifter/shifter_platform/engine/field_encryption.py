"""Native provisioning specs reuse the shared encrypted JSON field."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.field_encryption import EncryptedJSONField

if TYPE_CHECKING:
    pass


class EncryptedProvisioningSpecField(EncryptedJSONField):
    """Encrypt native registration secrets inside the versioned spec envelope."""

    sensitive_keys = frozenset({"authcode", "scm_pin_value", "otp_value"})


def transform_spec(value: object, *, encrypt: bool) -> object:
    """Retain the data migration helper using the shared field implementation."""
    field = EncryptedProvisioningSpecField()
    transform = field.get_prep_value if encrypt else field.to_python
    return transform({"spec": value})["spec"]
