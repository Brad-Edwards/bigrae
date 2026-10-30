"""Refresh the OpenFGA datastore credentials and restart the policy servers (#2313).

On AWS the released OpenFGA server uses rotated PostgreSQL passwords. The
alternating-users rotation keeps the previous credential valid until the next
rotation. This weekly Job copies the current credential of each duty from
Secrets Manager into its named Kubernetes Secret, then restarts the runtime and
admin servers so they read it at startup.

Its Kubernetes Role allows only ``patch`` on the three named Secrets and the two
named Deployments. Its IAM role reads only the three datastore secrets. It
never logs or returns a credential.

Usage: ``python -m shared.cloud.policy_credential_refresh`` with
``OPENFGA_DATASTORE_LOGIN_ARNS`` (JSON) and ``AWS_REGION`` in the environment.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

NAMESPACE = "shifter-platform"
DUTIES = ("runtime", "admin", "migrator")
SERVERS = ("openfga", "openfga-admin")
RESTART_ANNOTATION = "shifter.dev/policy-credentials-refreshed-at"
_LOGIN = re.compile(r"[a-z][a-z0-9_]{0,62}", flags=re.ASCII)
_SECRET_ARN = re.compile(r"arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+", flags=re.ASCII)


class RefreshError(RuntimeError):
    """A bounded refresh failure without credential or provider detail."""


class SecretReader(Protocol):
    """The Secrets Manager read that the refresh uses."""

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:
        """Return one secret version."""


class SecretPatcher(Protocol):
    """The Kubernetes core operation that the refresh uses."""

    def patch_namespaced_secret(self, name: str, namespace: str, body: Mapping[str, object]) -> object:
        """Patch one named Secret."""


class DeploymentPatcher(Protocol):
    """The Kubernetes apps operation that the refresh uses."""

    def patch_namespaced_deployment(self, name: str, namespace: str, body: Mapping[str, object]) -> object:
        """Patch one named Deployment."""


def secret_name(duty: str) -> str:
    """Return the Kubernetes Secret that carries one duty's datastore login."""
    return f"openfga-{duty}-datastore"


def parse_references(raw: str) -> dict[str, str]:
    """Parse the closed duty-to-secret-ARN map."""
    try:
        references = json.loads(raw)
    except (TypeError, ValueError):
        raise RefreshError("Datastore secret references are invalid") from None
    if (
        not isinstance(references, dict)
        or set(references) != set(DUTIES)
        or not all(isinstance(value, str) and _SECRET_ARN.fullmatch(value) for value in references.values())
    ):
        raise RefreshError("Datastore secret references are invalid")
    return {duty: references[duty] for duty in DUTIES}


def _credential(secrets: SecretReader, arn: str) -> dict[str, str]:
    """Read and validate one current credential; provider text never escapes."""
    try:
        value = json.loads(secrets.get_secret_value(SecretId=arn)["SecretString"])
    except (ClientError, BotoCoreError, ValueError, KeyError, TypeError):
        raise RefreshError("Datastore credential is unavailable") from None
    username = value.get("username") if isinstance(value, dict) else None
    password = value.get("password") if isinstance(value, dict) else None
    if (
        not isinstance(username, str)
        or not _LOGIN.fullmatch(username)
        or not isinstance(password, str)
        or not password
        or not password.isascii()
        or any(character.isspace() for character in password)
    ):
        raise RefreshError("Datastore credential has an unexpected shape")
    return {"username": username, "password": password}


def refresh(
    secrets: SecretReader,
    core: SecretPatcher,
    apps: DeploymentPatcher,
    references: dict[str, str],
    *,
    now: str,
    api_errors: tuple[type[Exception], ...],
) -> None:
    """Validate every credential first, then patch the Secrets and restart the servers.

    ``api_errors`` are the Kubernetes client's request and transport errors.
    """
    if set(references) != set(DUTIES):
        raise RefreshError("Datastore secret references are invalid")
    credentials = {duty: _credential(secrets, references[duty]) for duty in DUTIES}
    publication_errors: tuple[type[Exception], ...] = (*api_errors, OSError)
    try:
        for duty in DUTIES:
            core.patch_namespaced_secret(secret_name(duty), NAMESPACE, {"stringData": credentials[duty]})
        restart = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: now}}}}}
        for server in SERVERS:
            apps.patch_namespaced_deployment(server, NAMESPACE, restart)
    except publication_errors:
        raise RefreshError("Policy credential publication failed") from None


def main() -> int:
    """Run one refresh with the native SDK credential chain and in-cluster RBAC."""
    import boto3
    from kubernetes import client, config
    from kubernetes.client.exceptions import OpenApiException
    from kubernetes.config import ConfigException
    from urllib3.exceptions import HTTPError

    try:
        references = parse_references(os.environ.get("OPENFGA_DATASTORE_LOGIN_ARNS", ""))
        config.load_incluster_config()
        refresh(
            boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"]),
            client.CoreV1Api(),
            client.AppsV1Api(),
            references,
            now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            api_errors=(OpenApiException, HTTPError),
        )
    except RefreshError as error:
        logger.error("policy credential refresh failed: %s", error)
        return 1
    except (ConfigException, BotoCoreError, KeyError):
        # In-cluster configuration, SDK setup or a missing region; no detail is logged.
        logger.error("policy credential refresh failed")
        return 1
    logger.info("policy credentials refreshed; policy servers restarting")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
