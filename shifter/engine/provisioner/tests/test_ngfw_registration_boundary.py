"""Raw SQL NGFW inputs reach native bootstrap builders as usable secrets."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from shared.persisted_envelope import ensure_wrapped_persisted_spec

from gdc_vmseries_assets import _build_init_cfg
from ngfw_terraform_state import _build_tf_variables
from provisioner_db_ngfw import get_ngfw_data_by_request_id


@pytest.mark.parametrize("wrapped", [False, True])
def test_encrypted_sql_specs_are_decoded_before_native_bootstrap(mock_psycopg_connect, monkeypatch, wrapped):
    key = Fernet.generate_key()
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", key.decode())
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_USER", "postgres")
    monkeypatch.setenv("DB_NAME", "synthetic")
    monkeypatch.setenv("DB_PASSWORD", "synthetic")
    monkeypatch.setenv("SECRETS_KMS_KEY_ARN", "synthetic-key")

    def encrypted(value):
        return "enc:v1:" + Fernet(key).encrypt(value.encode()).decode()

    app = {"user_id": 42, "authcode": encrypted("SYNTHETIC_AUTH"), "scm_pin_value": encrypted("SYNTHETIC_PIN")}
    instance = {"ngfw_app": app, "items": [{"otp_value": encrypted("SYNTHETIC_OTP")}]}
    if wrapped:
        app = ensure_wrapped_persisted_spec("app.panw-ngfw", app)
        instance = ensure_wrapped_persisted_spec("instance.panw-ngfw", instance)
    _, _, cursor = mock_psycopg_connect
    request, native = str(uuid4()), str(uuid4())
    cursor.fetchone.return_value = (request, native, str(uuid4()), instance, app, {}, "provisioning")
    data = get_ngfw_data_by_request_id(request)
    variables = _build_tf_variables(request, native, data["app_spec"])
    assert variables["authcode"] == "SYNTHETIC_AUTH"
    assert variables["scm_pin_value"] == "SYNTHETIC_PIN"
    assert "vm-series-auto-registration-pin-value=SYNTHETIC_PIN" in _build_init_cfg(
        hostname="synthetic-ngfw", app_spec=data["app_spec"]
    )
    assert data["spec"]["items"][0]["otp_value"] == "SYNTHETIC_OTP"


def test_service_device_terraform_inputs_use_native_resource_identity(monkeypatch):
    monkeypatch.setenv("SECRETS_KMS_KEY_ARN", "synthetic-key")
    instance = str(uuid4())
    variables = _build_tf_variables(str(uuid4()), instance, {"user_id": None})
    assert variables["user_id"] is None
    assert variables["name_prefix"] == "ngfw-device-" + instance
    assert "None" not in variables["name_prefix"]


@pytest.mark.parametrize("failure", ["missing-key", "wrong-key", "malformed"])
def test_encrypted_native_inputs_fail_closed_without_valid_decryption(mock_psycopg_connect, monkeypatch, failure):
    from config._crypto import FieldDecryptError

    key = Fernet.generate_key()
    ciphertext = "enc:v1:" + Fernet(key).encrypt(b"SYNTHETIC_REGISTRATION").decode()
    if failure == "missing-key":
        monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)
    else:
        monkeypatch.setenv(
            "FIELD_ENCRYPTION_KEY", Fernet.generate_key().decode() if failure == "wrong-key" else key.decode()
        )
    if failure == "malformed":
        ciphertext = "enc:v1:malformed"
    for name, value in {
        "DB_HOST": "127.0.0.1",
        "DB_USER": "postgres",
        "DB_NAME": "synthetic",
        "DB_PASSWORD": "synthetic",
    }.items():
        monkeypatch.setenv(name, value)
    _, _, cursor = mock_psycopg_connect
    cursor.fetchone.return_value = (
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        {},
        {"authcode": ciphertext},
        {},
        "provisioning",
    )
    with pytest.raises(FieldDecryptError) as caught:
        get_ngfw_data_by_request_id(str(uuid4()))
    assert "SYNTHETIC_REGISTRATION" not in str(caught.value)
    assert ciphertext not in str(caught.value)
