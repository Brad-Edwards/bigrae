"""AWS RDS login provisioning for the existing PostgreSQL duties (#2313).

RDS has no Terraform user resource, so the deployment owner creates logins from
inside the cluster. Each runtime workload has its own RDS IAM login; it joins
the existing ``portal_runtime`` duty through the shared GCP reconciler. The
OpenFGA duties use password logins, because the released OpenFGA server cannot
create RDS IAM tokens.

The portal RDS logs every statement. A password therefore reaches the server
only as a SCRAM-SHA-256 verifier that libpq computes on the client. Every master
connection verifies the server certificate and host name with the RDS CA.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from typing import Any, Protocol

from psycopg import sql
from psycopg.rows import TupleRow

AWS_APPLICATION_WORKLOADS = frozenset({"portal", "workers", "ctf-scheduler", "provisioner-launcher"})
OPENFGA_LOGINS = {
    "openfga-runtime": "openfga_runtime",
    "openfga-admin": "openfga_admin",
    "openfga-migrator": "openfga_migrator",
}
_LOGIN = re.compile(r"[a-z][a-z0-9_]{0,62}", flags=re.ASCII)
_RESERVED = frozenset({"portal_runtime", "provisioner_lambda", "guacamole_admin", "rds_iam", "postgres"})


class _Cursor(Protocol):
    """The database cursor operations that the duty setup uses."""

    def execute(self, query: str | sql.SQL | sql.Composed, params: list[str] | None = None, /) -> object: ...

    def fetchone(self) -> TupleRow | None: ...


class _PgConn(Protocol):
    """The libpq operation that computes a password verifier on the client."""

    def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes | None = None) -> bytes: ...


def parse_iam_users(raw: str) -> dict[str, str]:
    """Parse the closed workload-to-login map rendered from Terraform."""
    try:
        users = json.loads(raw or "{}")
    except ValueError:
        raise ValueError("AWS database login map is invalid") from None
    if (
        not isinstance(users, dict)
        or set(users) != AWS_APPLICATION_WORKLOADS
        or not all(isinstance(login, str) and _LOGIN.fullmatch(login) for login in users.values())
        or len(set(users.values())) != len(users)
        or set(users.values()) & _RESERVED
    ):
        raise ValueError("AWS database login map is invalid")
    return dict(users)


def _role_exists(cursor: _Cursor, role: str) -> bool:
    """Return whether the database role exists."""
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
    return cursor.fetchone() is not None


def ensure_iam_logins(cursor: _Cursor, users: Mapping[str, str]) -> None:
    """Create each workload's RDS IAM login once; it holds no password."""
    if not _role_exists(cursor, "rds_iam"):
        raise ValueError("RDS IAM authentication is not available on this instance")
    for login in users.values():
        if not _role_exists(cursor, login):
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(login)))
        cursor.execute(sql.SQL("GRANT rds_iam TO {}").format(sql.Identifier(login)))


def ensure_password_login(cursor: _Cursor, pgconn: _PgConn, credential: Mapping[str, Any], base_login: str) -> bool:
    """Create the base OpenFGA login from its current secret once; rotation owns it later.

    Returns whether the login was created. An existing login keeps its password:
    the rotation function changes it. A secret that already names the clone means
    rotation has run, so the base login must exist.
    """
    username = credential.get("username")
    password = credential.get("password")
    if username not in {base_login, f"{base_login}_clone"} or not isinstance(password, str) or not password:
        raise ValueError("OpenFGA datastore credential does not match its duty")
    if _role_exists(cursor, base_login):
        return False
    if username != base_login:
        raise ValueError("OpenFGA datastore login is missing after rotation")
    verifier = password_verifier(pgconn, base_login, password)
    cursor.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(base_login), sql.Literal(verifier))
    )
    return True


def password_verifier(pgconn: _PgConn, login: str, password: str) -> str:
    """Return the SCRAM-SHA-256 verifier for one login password."""
    return pgconn.encrypt_password(password.encode(), login.encode(), b"scram-sha-256").decode("ascii")


def rds_ca_path(environ: Mapping[str, str] = os.environ) -> str:
    """Return the RDS CA bundle file; write the published bundle once if needed.

    The AWS renderer publishes the region's public RDS CA bundle as base64
    runtime configuration. The file is owner read-only and named by its digest.
    """
    configured = environ.get("DB_SSL_ROOT_CERT", "").strip()
    if configured:
        return configured
    try:
        # Malformed base64 raises binascii.Error, which is a ValueError.
        bundle = base64.b64decode(environ.get("DB_SSL_ROOT_CERT_PEM_B64", "").strip(), validate=True)
    except ValueError:
        raise ValueError("RDS CA bundle is not valid base64") from None
    if b"-----BEGIN CERTIFICATE-----" not in bundle:
        raise ValueError("RDS CA bundle holds no certificate")
    directory = tempfile.gettempdir()
    path = os.path.join(directory, "rds-ca-" + hashlib.sha256(bundle).hexdigest()[:16] + ".pem")
    if not os.path.exists(path):
        descriptor, staged = tempfile.mkstemp(dir=directory, suffix=".pem")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(bundle)
        # Only this process's user reads the bundle; it stays read-only.
        os.chmod(staged, 0o400)
        os.replace(staged, path)
    return path


def master_connection_parameters(master: Mapping[str, Any], environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """Return verified-TLS psycopg parameters for the RDS master credential.

    The master password may only go to a server that proves the RDS identity.
    """
    if environ.get("DB_SSLMODE", "").strip() != "verify-full":
        raise ValueError("RDS master connections require verify-full TLS")
    return {
        "host": environ.get("DB_HOST") or master["host"],
        "port": str(environ.get("DB_PORT") or master.get("port", 5432)),
        "user": master["username"],
        "password": master["password"],
        "dbname": master["dbname"],
        "sslmode": "verify-full",
        "sslrootcert": rds_ca_path(environ),
        "connect_timeout": 15,
    }
