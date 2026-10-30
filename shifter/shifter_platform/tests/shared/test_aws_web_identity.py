"""AWS STS outbound web identity verification against a synthetic account issuer."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from shared.aws_web_identity import valid_issuer, valid_role_arn, verify_aws_web_identity_token

ISSUER = "https://11111111-2222-3333-4444-555555555555.tokens.sts.global.api.aws"
AUDIENCE = "https://portal.example.test"
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/service/synthetic-agent"


@dataclass
class _Key:
    key: Any


class _KeySource:
    """Serve one public key, as the issuer's key set would."""

    def __init__(self, public_key: Any) -> None:
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _Key:
        return _Key(self.public_key)


@pytest.fixture(scope="module")
def issuer_keys() -> dict[str, Any]:
    return {
        "ES384": ec.generate_private_key(ec.SECP384R1()),
        "RS256": rsa.generate_private_key(public_exponent=65537, key_size=2048),
    }


def _claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": ROLE,
        "iat": now,
        "exp": now + 900,
        "jti": "synthetic-token-id",
        "https://sts.amazonaws.com/": {"aws_account": ACCOUNT, "source_region": "us-east-2"},
    }
    claims.update(changes)
    return {key: value for key, value in claims.items() if value is not None}


def _verify(token: str, public_key: Any) -> str:
    return verify_aws_web_identity_token(
        token, issuer=ISSUER, audience=AUDIENCE, account_id=ACCOUNT, keys=_KeySource(public_key)
    )


def test_accepts_exact_role_tokens_for_both_documented_algorithms(issuer_keys):
    for algorithm, private_key in issuer_keys.items():
        token = jwt.encode(_claims(), private_key, algorithm=algorithm)
        assert _verify(token, private_key.public_key()) == ROLE
    single_list_audience = jwt.encode(_claims(aud=[AUDIENCE]), issuer_keys["ES384"], algorithm="ES384")
    assert _verify(single_list_audience, issuer_keys["ES384"].public_key()) == ROLE


def test_refuses_substituted_claims_lifetimes_and_principals(issuer_keys):
    private_key = issuer_keys["ES384"]
    now = int(time.time())
    substitutions = (
        {"aud": "https://other.example.test"},
        {"aud": [AUDIENCE, "https://other.example.test"]},
        {"iss": "https://99999999-2222-3333-4444-555555555555.tokens.sts.global.api.aws"},
        {"sub": "arn:aws:iam::111122223333:role/service/synthetic-agent"},
        {"sub": f"arn:aws:iam::{ACCOUNT}:user/synthetic-person"},
        {"sub": f"arn:aws:sts::{ACCOUNT}:assumed-role/synthetic-agent/session"},
        {"https://sts.amazonaws.com/": {"aws_account": "111122223333"}},
        {"https://sts.amazonaws.com/": None},
        {"jti": None},
        {"iat": now - 7200, "exp": now + 60},
        {"iat": now - 3600, "exp": now - 120},
    )
    for change in substitutions:
        token = jwt.encode(_claims(**change), private_key, algorithm="ES384")
        with pytest.raises(ValueError, match=r"^AWS web identity denied$"):
            _verify(token, private_key.public_key())


def test_refuses_wrong_keys_and_unapproved_algorithms(issuer_keys):
    token = jwt.encode(_claims(), issuer_keys["ES384"], algorithm="ES384")
    other = ec.generate_private_key(ec.SECP384R1())
    with pytest.raises(ValueError):
        _verify(token, other.public_key())
    hmac_token = jwt.encode(_claims(), "synthetic-shared-secret-value-1234567890", algorithm="HS256")
    with pytest.raises(ValueError):
        _verify(hmac_token, "synthetic-shared-secret-value-1234567890")
    with pytest.raises(ValueError):
        verify_aws_web_identity_token(
            token,
            issuer="https://attacker.example.test",
            audience=AUDIENCE,
            account_id=ACCOUNT,
            keys=_KeySource(issuer_keys["ES384"].public_key()),
        )


def test_key_source_failure_is_denial_without_detail(issuer_keys):
    class _Unavailable:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise RuntimeError("synthetic-secret-marker")

    token = jwt.encode(_claims(), issuer_keys["ES384"], algorithm="ES384")
    with pytest.raises(ValueError) as raised:
        verify_aws_web_identity_token(token, issuer=ISSUER, audience=AUDIENCE, account_id=ACCOUNT, keys=_Unavailable())
    assert "synthetic-secret-marker" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_issuer_and_role_grammar():
    assert valid_issuer(ISSUER)
    for value in (
        "http://x.tokens.sts.global.api.aws",
        "https://tokens.sts.global.api.aws.attacker.test",
        f"{ISSUER}/path",
        f"{ISSUER}?q=1",
        None,
    ):
        assert not valid_issuer(value)
    assert valid_role_arn(ROLE, account_id=ACCOUNT)
    assert not valid_role_arn(ROLE, account_id="111122223333")
    assert not valid_role_arn(f"arn:aws:iam::{ACCOUNT}:user/person", account_id=ACCOUNT)
    # IAM allows any printable ASCII in a role path and bounds path and name lengths.
    prefix = f"arn:aws:iam::{ACCOUNT}:role/"
    assert valid_role_arn(prefix + "synthetic-agent", account_id=ACCOUNT)
    assert valid_role_arn(prefix + "team!~#/a:b/synthetic-agent", account_id=ACCOUNT)
    assert valid_role_arn(prefix + "p" * 510 + "/" + "n" * 64, account_id=ACCOUNT)
    for subject in (
        prefix + "p" * 511 + "/synthetic-agent",
        prefix + "service/" + "n" * 65,
        prefix + "service/synthetic agent",
        prefix + "service/synthetic~agent",
        prefix + "service/",
    ):
        assert not valid_role_arn(subject, account_id=ACCOUNT)
