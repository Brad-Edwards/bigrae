"""AWS provisioner connections verify the RDS server identity (#2313)."""

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from provisioner_db import get_db_connection

CA_PEM = "-----BEGIN CERTIFICATE-----\nU3ludGhldGljIFJEUyBDQQ==\n-----END CERTIFICATE-----\n"


@pytest.fixture
def aws_env(monkeypatch, tmp_path):
    for name in ("DB_PASSWORD", "DB_AUTH_MODE", "CLOUD_REGION"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "CLOUD_PROVIDER": "aws",
        "AWS_REGION": "us-east-2",
        "DB_HOST": "db.example.internal",
        "DB_PORT": "5432",
        "DB_NAME": "shifter",
        "DB_USER": "provisioner_lambda",
        "DB_SSLMODE": "verify-full",
        "DB_SSL_ROOT_CERT_PEM_B64": base64.b64encode(CA_PEM.encode()).decode(),
        "TMPDIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)


class _Rds:
    """The boto3 RDS client boundary that signs IAM authentication tokens."""

    def generate_db_auth_token(self, **kwargs: object) -> str:
        return "synthetic-iam-token"


def test_iam_connection_verifies_certificate_and_host(aws_env):
    with patch("boto3.client", return_value=_Rds()), patch("psycopg.connect") as connect:
        get_db_connection()
    kwargs = connect.call_args.kwargs
    assert kwargs["sslmode"] == "verify-full"
    assert Path(kwargs["sslrootcert"]).read_text() == CA_PEM
    assert kwargs["password"] == "synthetic-iam-token"


@pytest.mark.parametrize(
    ("name", "value"),
    [("DB_SSLMODE", "require"), ("DB_SSL_ROOT_CERT_PEM_B64", ""), ("DB_SSL_ROOT_CERT_PEM_B64", "not-base64!")],
)
def test_iam_connection_refuses_unverified_tls(aws_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with (
        patch("boto3.client", return_value=_Rds()),
        patch("psycopg.connect") as connect,
        pytest.raises(RuntimeError, match="RDS"),
    ):
        get_db_connection()
    connect.assert_not_called()
