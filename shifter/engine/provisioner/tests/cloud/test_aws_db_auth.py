"""RDS IAM token generation reports provider failures without credential detail."""

import logging
import traceback
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from cloud.aws.db_auth import AWSDBAuth
from cloud.exceptions import CloudDBAuthError


def test_provider_error_cannot_disclose_credentials(monkeypatch, caplog):
    marker = "synthetic-sensitive-credential-2313"
    client = Mock()
    client.generate_db_auth_token.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": marker}}, "GenerateDBAuthToken"
    )
    adapter = AWSDBAuth()
    monkeypatch.setattr(adapter, "_get_client", lambda: client)
    with caplog.at_level(logging.DEBUG), pytest.raises(CloudDBAuthError) as error:
        adapter.generate_auth_token("db.example.test", 5432, "runtime")
    assert marker not in caplog.text
    assert marker not in "".join(traceback.format_exception(error.value))
    assert str(error.value) == "Failed to generate RDS IAM auth token"
