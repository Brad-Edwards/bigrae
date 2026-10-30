"""Bounded Cognito user-state reads for the session provider recheck.

The configured OIDC issuer names exactly one user pool. The portal reads one
user's state with ``AdminGetUser``; its IAM role allows no other directory
operation. Provider failures propagate so session admission fails closed.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

_ISSUER = re.compile(
    r"https://cognito-idp\.(?P<region>[a-z]{2}(?:-[a-z]+)+-\d)\.amazonaws\.com/(?P<pool>[a-z]{2}(?:-[a-z]+)+-\d_[A-Za-z0-9]{1,55})",
    flags=re.ASCII,
)
_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class CognitoUserState:
    """The directory facts that keep a Cognito session admissible."""

    enabled: bool
    status: str
    email_verified: bool
    subject: str


def user_pool(issuer: str) -> tuple[str, str]:
    """Return the region and pool ID that the configured issuer names."""
    match = _ISSUER.fullmatch(issuer) if isinstance(issuer, str) else None
    if match is None or not match.group("pool").startswith(f"{match.group('region')}_"):
        raise ValueError("Cognito issuer is not a user pool issuer")
    return match.group("region"), match.group("pool")


class _CognitoErrors(Protocol):
    UserNotFoundException: type[Exception]


class CognitoClient(Protocol):
    """The Cognito operation that the session recheck uses."""

    exceptions: _CognitoErrors

    def admin_get_user(self, *, UserPoolId: str, Username: str) -> Mapping[str, Any]:
        """Return one user's lifecycle state."""


def _client(region: str) -> CognitoClient:
    """Return one cached Cognito client with bounded network timeouts."""
    client = _clients.get(region)
    if client is None:
        with _clients_lock:
            client = _clients.get(region)
            if client is None:
                import boto3
                from botocore.config import Config

                client = boto3.client(
                    "cognito-idp",
                    region_name=region,
                    config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
                )
                _clients[region] = client
    return client


def provider_user_state(issuer: str, subject: str) -> CognitoUserState:
    """Read one user's lifecycle state; a missing user is a disabled state."""
    region, pool = user_pool(issuer)
    client = _client(region)
    try:
        response = client.admin_get_user(UserPoolId=pool, Username=subject)
    except client.exceptions.UserNotFoundException:
        return CognitoUserState(enabled=False, status="", email_verified=False, subject="")
    attributes = {
        item.get("Name"): item.get("Value") for item in response.get("UserAttributes", []) if isinstance(item, dict)
    }
    return CognitoUserState(
        enabled=response.get("Enabled") is True,
        status=str(response.get("UserStatus", "")),
        email_verified=attributes.get("email_verified") == "true",
        subject=str(attributes.get("sub", "")),
    )
