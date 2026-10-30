"""Typed, bounded credential assurance policy."""

import os

from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "AWS_ACCOUNT_ID",
    "AWS_SERVICE_TOKEN_AUDIENCE",
    "AWS_SERVICE_TOKEN_ISSUER",
    "GCP_SERVICE_TOKEN_AUDIENCE",
    "IDENTITY_PLATFORM_TENANT_ID",
    "IDENTITY_SESSION_ABSOLUTE_SECONDS",
    "IDENTITY_SESSION_IDLE_SECONDS",
    "IDENTITY_SESSION_RECHECK_SECONDS",
]


def _bounded_seconds(value: str, minimum: int, maximum: int) -> int:
    """Parse one bounded positive credential-assurance duration."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ImproperlyConfigured("Credential time limits must be integers") from exc
    if not minimum <= parsed <= maximum:
        raise ImproperlyConfigured("Credential time limit outside supported bounds")
    return parsed


GCP_SERVICE_TOKEN_AUDIENCE = os.environ.get("GCP_SERVICE_TOKEN_AUDIENCE", "").strip()
# AWS STS outbound web identity: the account issuer, the installation audience
# and the account that owns the issuer. Deployment configuration supplies all
# three; a caller never does.
AWS_SERVICE_TOKEN_ISSUER = os.environ.get("AWS_SERVICE_TOKEN_ISSUER", "").strip()
AWS_SERVICE_TOKEN_AUDIENCE = os.environ.get("AWS_SERVICE_TOKEN_AUDIENCE", "").strip()
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "").strip()
IDENTITY_PLATFORM_TENANT_ID = os.environ.get("IDENTITY_PLATFORM_TENANT_ID", "").strip()
IDENTITY_SESSION_ABSOLUTE_SECONDS = _bounded_seconds(
    os.environ.get("IDENTITY_SESSION_ABSOLUTE_SECONDS", "28800"), 60, 86400
)
IDENTITY_SESSION_IDLE_SECONDS = _bounded_seconds(os.environ.get("IDENTITY_SESSION_IDLE_SECONDS", "1800"), 60, 3600)
IDENTITY_SESSION_RECHECK_SECONDS = _bounded_seconds(os.environ.get("IDENTITY_SESSION_RECHECK_SECONDS", "300"), 1, 300)
