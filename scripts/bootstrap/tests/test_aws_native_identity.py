"""AWS native identity deployment helpers (#2313)."""

from __future__ import annotations

import base64
import json

import pytest

import aws_native_identity

CA = b"-----BEGIN CERTIFICATE-----\nU3ludGhldGljIFJEUyBDQQ==\n-----END CERTIFICATE-----\n"
ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev"


def _outputs() -> dict[str, object]:
    return {
        "openfga_datastore_secret_arns": {
            "value": {duty: f"{ARN}/openfga-{duty}-datastore" for duty in ("runtime", "admin", "migrator")}
        }
    }


def _bundle(key: str, tls: str) -> str:
    return json.dumps({"server-preshared-keys": key, "api-token": key, "tls.crt": "crt", "tls.key": tls})


def test_policy_credentials_publish_named_objects_through_apply_only():
    settings = {
        "openfga": {
            "enabled": True,
            "runtime_secret_ref": f"{ARN}/runtime-bundle",
            "admin_secret_ref": f"{ARN}/admin-bundle",
            "ca_secret_ref": f"{ARN}/ca-bundle",
        }
    }
    values = {
        f"{ARN}/runtime-bundle": _bundle("r" * 40, "runtime-key"),
        f"{ARN}/admin-bundle": _bundle("a" * 40, "admin-key"),
        f"{ARN}/ca-bundle": json.dumps({"ca.crt": "public-ca"}),
        **{
            f"{ARN}/openfga-{duty}-datastore": json.dumps({"username": f"openfga_{duty}", "password": "pw-" + duty})
            for duty in ("runtime", "admin", "migrator")
        },
    }
    applied = []
    assert aws_native_identity.publish_policy_credentials(
        settings, _outputs(), read_secret=values.__getitem__, apply=applied.append
    )
    names = [(item["kind"], item["metadata"]["name"]) for item in applied]
    assert names == [
        ("Secret", "openfga-runtime"),
        ("Secret", "openfga-admin"),
        ("ConfigMap", "openfga-ca"),
        ("Secret", "openfga-runtime-datastore"),
        ("Secret", "openfga-admin-datastore"),
        ("Secret", "openfga-migrator-datastore"),
    ]
    assert base64.b64decode(applied[0]["data"]["runtime-preshared-key"]).decode() == "r" * 40
    assert applied[3]["stringData"] == {"username": "openfga_runtime", "password": "pw-runtime"}
    values[f"{ARN}/admin-bundle"] = _bundle("r" * 40, "admin-key")
    with pytest.raises(ValueError):
        aws_native_identity.publish_policy_credentials(
            settings, _outputs(), read_secret=values.__getitem__, apply=applied.append
        )
    assert not aws_native_identity.publish_policy_credentials(
        {"openfga": {"enabled": False}}, _outputs(), read_secret=values.__getitem__, apply=applied.append
    )
