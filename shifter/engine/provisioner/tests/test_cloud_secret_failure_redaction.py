"""Secret-store failures retain classification without exposing provider payloads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from cloud.aws.secrets import AWSSecretsStore
from cloud.exceptions import CloudSecretsError
from cloud.gcp.secrets import GCPSecretsStore

_PRIVATE = "SYNTHETIC_PRIVATE_SECRET_VALUE"
_REFERENCE = "projects/synthetic-project/secrets/synthetic-credential"


@pytest.mark.parametrize("provider", ["aws", "gcp"])
def test_secret_store_failures_omit_provider_payloads_and_raw_secret_references(provider, monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=f"cloud.{provider}.secrets")
    if provider == "aws":
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": _PRIVATE}}, "GetSecretValue")
        client = SimpleNamespace(get_secret_value=Mock(side_effect=error))
        monkeypatch.setattr("cloud.aws.base.boto3.client", lambda *args, **kwargs: client)
        store = AWSSecretsStore()
    else:
        client = SimpleNamespace(access_secret_version=Mock(side_effect=RuntimeError(_PRIVATE)))
        sdk = SimpleNamespace(SecretManagerServiceClient=lambda: client)
        monkeypatch.setattr("cloud.gcp.secrets.import_google_module", lambda name: sdk)
        store = GCPSecretsStore()

    with pytest.raises(CloudSecretsError) as raised:
        store.get_secret(_REFERENCE)

    assert _PRIVATE not in str(raised.value)
    assert _PRIVATE not in caplog.text
    assert _REFERENCE not in caplog.text
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    [record] = [record for record in caplog.records if record.levelname == "ERROR"]
    assert "secret_id_fp=" in record.getMessage()
    assert "exc_type=" in record.getMessage()
    assert not record.exc_info
