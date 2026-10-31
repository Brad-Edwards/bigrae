"""Secrets Manager rotation for the OpenFGA PostgreSQL logins (#2313).

This function follows the four documented Secrets Manager rotation steps:
createSecret, setSecret, testSecret and finishSecret.

The ``ROTATION_STRATEGY`` environment value selects the strategy:

* ``alternating``: the runtime and admin logins. The secret alternates between
  ``<login>`` and ``<login>_clone``. The master credential sets the inactive
  login's password. The clone is created once as a member of the original
  login, so it holds the same duty. The previous credential stays valid until
  the next rotation.
* ``single``: the migration login. The login changes its own password with its
  current credential. Only deploy Jobs use it.

Deployment configuration, not secret content, decides which SQL login each
secret may hold (``ROTATION_LOGINS``) and which master secret the alternating
strategy uses (``MASTER_SECRET_ARN``). A principal that can write one secret
therefore cannot make the master credential change another duty's login.

Every database connection verifies the server certificate and host name with
the bundled RDS CA. No password, secret value or provider error text is logged.
The portal RDS logs every statement, so a password reaches the server only as a
SCRAM-SHA-256 verifier that libpq computes on the client.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import psycopg
from psycopg import sql

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_CLONE_SUFFIX = "_clone"
_MAX_IDENTIFIER = 63
_EXCLUDE_CHARACTERS = "/@\"'\\:?#[]%&+ "
_CA_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rds-ca-bundle.pem")
_REQUIRED_FIELDS = ("engine", "host", "username", "password", "dbname", "port")


class RotationError(RuntimeError):
    """A bounded rotation failure without secret or provider detail."""


def _client() -> Any:
    return boto3.client("secretsmanager")


def _secret(client: Any, arn: str, stage: str, token: str | None = None) -> dict[str, Any]:
    """Read and validate one secret version's credential JSON."""
    arguments: dict[str, Any] = {"SecretId": arn, "VersionStage": stage}
    if token is not None:
        arguments["VersionId"] = token
    value = json.loads(client.get_secret_value(**arguments)["SecretString"])
    if not isinstance(value, dict) or any(field not in value for field in _REQUIRED_FIELDS):
        raise RotationError("Rotation secret has an unexpected structure")
    if value["engine"] != "postgres":
        raise RotationError("Rotation secret is not a PostgreSQL credential")
    return value


def _connect(credential: dict[str, Any]) -> psycopg.Connection:
    """Open a verified TLS connection; the CA bundle ships in the package."""
    return psycopg.connect(
        host=credential["host"],
        port=int(credential["port"]),
        dbname=credential["dbname"],
        user=credential["username"],
        password=credential["password"],
        sslmode="verify-full",
        sslrootcert=_CA_BUNDLE,
        connect_timeout=5,
    )


def _verifier(connection: psycopg.Connection, username: str, password: str) -> sql.Composable:
    """Return a SCRAM-SHA-256 verifier literal so logs never hold the password."""
    encrypted = connection.pgconn.encrypt_password(password.encode(), username.encode(), b"scram-sha-256")
    return sql.Literal(encrypted.decode("ascii"))


def _alternate_username(username: str) -> str:
    """Return the other login of the alternating pair."""
    if username.endswith(_CLONE_SUFFIX):
        return username[: -len(_CLONE_SUFFIX)]
    if len(username) + len(_CLONE_SUFFIX) > _MAX_IDENTIFIER:
        raise RotationError("Login name is too long for an alternating clone")
    return username + _CLONE_SUFFIX


def _strategy() -> str:
    strategy = os.environ.get("ROTATION_STRATEGY", "")
    if strategy not in {"alternating", "single"}:
        raise RotationError("Rotation strategy is not configured")
    return strategy


def _assigned_logins(arn: str) -> frozenset[str]:
    """Return the only SQL logins that this secret may hold."""
    try:
        base = json.loads(os.environ.get("ROTATION_LOGINS", ""))[arn]
    except (ValueError, KeyError, TypeError):
        raise RotationError("Rotation secret has no assigned login") from None
    if not isinstance(base, str) or not base:
        raise RotationError("Rotation secret has no assigned login")
    if _strategy() == "alternating":
        return frozenset({base, _alternate_username(base)})
    return frozenset({base})


def _require_assigned(arn: str, *credentials: dict[str, Any]) -> None:
    allowed = _assigned_logins(arn)
    if any(credential["username"] not in allowed for credential in credentials):
        raise RotationError("Rotation secret holds a login that is not assigned to it")


def create_secret(client: Any, arn: str, token: str) -> None:
    """Stage a new credential as AWSPENDING unless this token already has one."""
    current = _secret(client, arn, "AWSCURRENT")
    _require_assigned(arn, current)
    try:
        _secret(client, arn, "AWSPENDING", token)
        return
    except client.exceptions.ResourceNotFoundException:
        pass
    pending = dict(current)
    if _strategy() == "alternating":
        pending["username"] = _alternate_username(current["username"])
    pending["password"] = client.get_random_password(
        PasswordLength=32, ExcludeCharacters=_EXCLUDE_CHARACTERS, RequireEachIncludedType=True
    )["RandomPassword"]
    client.put_secret_value(
        SecretId=arn, ClientRequestToken=token, SecretString=json.dumps(pending), VersionStages=["AWSPENDING"]
    )
    logger.info("createSecret: staged a pending credential")


def _set_alternating(client: Any, current: dict[str, Any], pending: dict[str, Any]) -> None:
    # The configured master is authoritative; the secret's masterarn is ignored.
    master_arn = os.environ.get("MASTER_SECRET_ARN", "")
    if not master_arn:
        raise RotationError("Alternating rotation requires the master secret reference")
    master = _secret(client, master_arn, "AWSCURRENT")
    with _connect(master) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [pending["username"]])
        target = sql.Identifier(pending["username"])
        verifier = _verifier(connection, pending["username"], pending["password"])
        if cursor.fetchone() is None:
            cursor.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(target, verifier))
            cursor.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(current["username"]), target))
        else:
            cursor.execute(sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(target, verifier))


def _set_single(current: dict[str, Any], pending: dict[str, Any]) -> None:
    if pending["username"] != current["username"]:
        raise RotationError("Single-user rotation cannot change the login")
    for credential in (pending, current):
        try:
            with _connect(credential) as connection, connection.cursor() as cursor:
                verifier = _verifier(connection, pending["username"], pending["password"])
                cursor.execute(
                    sql.SQL("ALTER ROLE {} WITH PASSWORD {}").format(sql.Identifier(pending["username"]), verifier)
                )
            return
        except psycopg.OperationalError:
            # A retry after a lost acknowledgement may already use the pending
            # password; the current password is the normal path.
            continue
    raise RotationError("Single-user rotation could not authenticate")


def set_secret(client: Any, arn: str, token: str) -> None:
    """Apply the pending password to the database."""
    current = _secret(client, arn, "AWSCURRENT")
    pending = _secret(client, arn, "AWSPENDING", token)
    _require_assigned(arn, current, pending)
    if (pending["host"], pending["dbname"], str(pending["port"])) != (
        current["host"],
        current["dbname"],
        str(current["port"]),
    ):
        raise RotationError("Pending credential changes the database target")
    if _strategy() == "alternating":
        _set_alternating(client, current, pending)
    else:
        _set_single(current, pending)
    logger.info("setSecret: applied the pending credential")


def check_secret(client: Any, arn: str, token: str) -> None:
    """Sign in with the pending credential before it becomes current."""
    pending = _secret(client, arn, "AWSPENDING", token)
    _require_assigned(arn, pending)
    with _connect(pending) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    logger.info("testSecret: the pending credential signs in")


def finish_secret(client: Any, arn: str, token: str) -> None:
    """Move AWSCURRENT to the tested version."""
    stages = client.describe_secret(SecretId=arn)["VersionIdsToStages"]
    current_version = next((version for version, labels in stages.items() if "AWSCURRENT" in labels), None)
    if current_version == token:
        return
    client.update_secret_version_stage(
        SecretId=arn, VersionStage="AWSCURRENT", MoveToVersionId=token, RemoveFromVersionId=current_version
    )
    logger.info("finishSecret: the tested credential is current")


_STEPS = {
    "createSecret": create_secret,
    "setSecret": set_secret,
    "testSecret": check_secret,
    "finishSecret": finish_secret,
}


def handler(event: dict[str, Any], context: object) -> None:
    """Run one rotation step for one secret version."""
    arn = event["SecretId"]
    token = event["ClientRequestToken"]
    step = _STEPS.get(event["Step"])
    if step is None:
        raise RotationError("Unknown rotation step")
    client = _client()
    metadata = client.describe_secret(SecretId=arn)
    if not metadata.get("RotationEnabled"):
        raise RotationError("Rotation is not enabled for this secret")
    stages = metadata.get("VersionIdsToStages", {})
    if token not in stages:
        raise RotationError("Rotation version is not staged for this secret")
    if "AWSCURRENT" in stages[token] and event["Step"] != "finishSecret":
        return
    if "AWSCURRENT" not in stages[token] and "AWSPENDING" not in stages[token]:
        raise RotationError("Rotation version is not pending")
    try:
        step(client, arn, token)
    except RotationError:
        raise
    except Exception:
        # Driver and provider errors can carry connection or credential detail.
        logger.error("%s: rotation step failed", event["Step"])
        raise RotationError("Rotation step failed") from None
