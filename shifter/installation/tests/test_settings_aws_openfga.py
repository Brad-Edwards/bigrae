"""AWS OpenFGA operator references are closed and separated (#2313)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from installation.settings_aws import AwsSettings

ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/openfga"


def _openfga(**changes: object) -> dict[str, object]:
    return {
        "enabled": True,
        "runtime_secret_ref": f"{ARN}-runtime",
        "admin_secret_ref": f"{ARN}-admin",
        "ca_secret_ref": f"{ARN}-ca",
        **changes,
    }


def test_enabled_policy_requires_distinct_exact_references_and_paired_ids():
    settings = AwsSettings.model_validate({"region": "us-east-2", "openfga": _openfga()})
    assert settings.openfga.enabled and settings.openfga.store_id == ""
    assert AwsSettings.model_validate({"region": "us-east-2"}).openfga.enabled is False
    for change in (
        {"admin_secret_ref": f"{ARN}-runtime"},
        {"ca_secret_ref": f"{ARN}-admin"},
        {"runtime_secret_ref": None},
        {"store_id": "01HZZZZZZZZZZZZZZZZZZZZZZZ"},
        {"runtime_secret_ref": "shifter/dev/openfga-runtime"},
        {"runtime_secret_ref": f"{ARN}-runtime:AWSCURRENT"},
        {"unexpected": True},
    ):
        with pytest.raises(ValidationError):
            AwsSettings.model_validate({"region": "us-east-2", "openfga": _openfga(**change)})
