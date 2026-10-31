"""Verify AWS STS outbound web identity tokens for one configured account.

AWS STS ``GetWebIdentityToken`` issues a signed JWT whose subject is the
caller's IAM principal ARN. The issuer URL is unique to one AWS account and
publishes its signing keys at ``/.well-known/jwks.json``. Deployment
configuration supplies the issuer, audience and account; a caller never does.
Only IAM role subjects are accepted: IAM users have long-term credentials.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from jwt import PyJWK

_ALGORITHMS = ("ES384", "RS256")
_MAX_TOKEN_BYTES = 16_384
_MAX_LIFETIME_SECONDS = 3600
_CLAIMS_NAMESPACE = "https://sts.amazonaws.com/"
_ACCOUNT = re.compile(r"\d{12}", flags=re.ASCII)
# IAM role paths hold printable ASCII up to 512 characters with both slashes;
# role names hold up to 64 characters from a closed set (IAM API Role type).
_ROLE_ARN = re.compile(
    r"arn:aws:iam::(?P<account>\d{12}):role/(?:[\x21-\x7e]{1,510}/)?[A-Za-z0-9_+=,.@-]{1,64}",
    flags=re.ASCII,
)
_DENIED = "AWS web identity denied"

_jwks_clients: dict[str, Any] = {}
_jwks_lock = threading.Lock()


class SigningKeySource(Protocol):
    """The PyJWKClient operation used to resolve a token's signing key."""

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        """Return the signing key that the token header names."""


def valid_issuer(issuer: object) -> bool:
    """Return whether the value is an STS outbound web identity issuer URL."""
    if not isinstance(issuer, str) or not issuer.isascii():
        return False
    parts = urlsplit(issuer)
    return (
        parts.scheme == "https"
        and (parts.hostname or "").endswith(".tokens.sts.global.api.aws")
        and parts.port is None
        and parts.path in {"", "/"}
        and not any((parts.username, parts.password, parts.query, parts.fragment))
    )


def valid_account(account_id: object) -> bool:
    """Return whether the value is a 12-digit AWS account ID."""
    return isinstance(account_id, str) and _ACCOUNT.fullmatch(account_id) is not None


def valid_role_arn(subject: object, *, account_id: str) -> bool:
    """Return whether the subject is an IAM role ARN in exactly this account."""
    if not isinstance(subject, str) or len(subject) > 2048:
        return False
    match = _ROLE_ARN.fullmatch(subject)
    return match is not None and match.group("account") == account_id


def _signing_keys(issuer: str) -> SigningKeySource:
    """Return one cached, bounded key-set client for the configured issuer."""
    from jwt import PyJWKClient

    client = _jwks_clients.get(issuer)
    if client is None:
        with _jwks_lock:
            client = _jwks_clients.get(issuer)
            if client is None:
                client = PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json", cache_keys=True, timeout=5)
                _jwks_clients[issuer] = client
    return client


def _exact_audience(claims: dict[str, Any], audience: str) -> bool:
    """Accept one intended recipient only, so the token is not usable elsewhere."""
    value = claims.get("aud")
    return value == audience or value == [audience]


def _valid_request(token: object, *, issuer: str, audience: str, account_id: str) -> bool:
    """Return whether the token and the deployment configuration are fit to verify."""
    return (
        isinstance(token, str)
        and token.isascii()
        and len(token) <= _MAX_TOKEN_BYTES
        and valid_issuer(issuer)
        and valid_account(account_id)
        and audience.startswith("https://")
    )


def _verified_subject(claims: dict[str, Any], *, audience: str, account_id: str) -> str:
    """Return the IAM role ARN from signature-checked claims, or raise ``ValueError``."""
    namespace = claims.get(_CLAIMS_NAMESPACE)
    subject = claims.get("sub")
    lifetime = claims["exp"] - claims["iat"]
    if (
        not _exact_audience(claims, audience)
        or not isinstance(namespace, dict)
        or namespace.get("aws_account") != account_id
        or not isinstance(subject, str)
        or not valid_role_arn(subject, account_id=account_id)
        or not isinstance(lifetime, int)
        or not 0 < lifetime <= _MAX_LIFETIME_SECONDS
    ):
        raise ValueError(_DENIED)
    return subject


def verify_aws_web_identity_token(
    token: str,
    *,
    issuer: str,
    audience: str,
    account_id: str,
    keys: SigningKeySource | None = None,
) -> str:
    """Return the verified IAM role ARN, or raise ``ValueError`` without detail."""
    import jwt

    try:
        if not _valid_request(token, issuer=issuer, audience=audience, account_id=account_id):
            raise ValueError(_DENIED)
        signing_key = (keys or _signing_keys(issuer)).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=list(_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]},
        )
        return _verified_subject(claims, audience=audience, account_id=account_id)
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        # PyJWT wraps key-set, signature and transport failures in PyJWTError.
        # Never surface key-set, token, claim or transport detail to callers.
        raise ValueError(_DENIED) from None
