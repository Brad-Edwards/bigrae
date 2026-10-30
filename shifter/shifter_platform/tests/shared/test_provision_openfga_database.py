"""OpenFGA database and login provisioning on AWS (#2313)."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from psycopg import sql

MODULE = "shared.management.commands.provision_openfga_database"
ARNS = {
    "runtime": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-runtime-datastore",
    "admin": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-admin-datastore",
    "migrator": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-migrator-datastore",
}
PASSWORD = "SyntheticDatastorePassword2313"
CA_PATH = "/run/rds-ca/ca.pem"


class _Cursor:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.executed: list[str] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query, params=None) -> None:
        text = query.as_string(None) if isinstance(query, sql.Composable) else query
        self.executed.append(text)
        if text.startswith("SELECT 1 FROM"):
            self._row = (1,) if params and params[0] in self.existing else None

    def fetchone(self):
        return self._row


class _PgConn:
    def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes | None = None) -> bytes:
        assert algorithm == b"scram-sha-256"
        return b"SCRAM-SHA-256$4096:synthetic-salt$" + user + b":synthetic-server"


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.pgconn = _PgConn()
        self.kwargs: dict[str, object] = {}

    def cursor(self):
        return self._cursor

    def close(self) -> None:
        return None


@pytest.fixture
def provisioning(monkeypatch):
    def build(existing: set[str], usernames: dict[str, str] | None = None):
        monkeypatch.setenv("CLOUD_PROVIDER", "aws")
        monkeypatch.setenv("DB_SECRET_ID", "master-arn")
        monkeypatch.setenv("DB_SSLMODE", "verify-full")
        monkeypatch.setenv("DB_SSL_ROOT_CERT", CA_PATH)
        monkeypatch.setenv("OPENFGA_DATASTORE_LOGIN_ARNS", json.dumps(ARNS))
        logins = {"runtime": "openfga_runtime", "admin": "openfga_admin", "migrator": "openfga_migrator"}
        logins.update(usernames or {})
        payloads = {
            "master-arn": json.dumps(
                {"host": "db.example.test", "port": 5432, "username": "master", "password": "m", "dbname": "shifter"}
            ),
            **{ARNS[duty]: json.dumps({"username": logins[duty], "password": PASSWORD}) for duty in ARNS},
        }
        monkeypatch.setattr(
            f"{MODULE}.get_secrets_store", lambda: type("S", (), {"get_secret": lambda self, ref: payloads[ref]})()
        )
        cursor = _Cursor(existing)
        connection = _Connection(cursor)

        def connect(**kwargs):
            connection.kwargs = kwargs
            return connection

        monkeypatch.setattr(f"{MODULE}.psycopg.connect", connect)
        return cursor, connection

    return build


def test_creates_database_and_base_logins_with_verifiers_only(provisioning):
    cursor, connection = provisioning(existing=set())
    out = StringIO()
    call_command("provision_openfga_database", stdout=out)
    assert 'CREATE DATABASE "openfga"' in cursor.executed
    created = [text for text in cursor.executed if text.startswith("CREATE ROLE")]
    assert [text.split('"')[1] for text in created] == ["openfga_runtime", "openfga_admin", "openfga_migrator"]
    assert all("SCRAM-SHA-256$4096:synthetic-salt$" in text for text in created)
    assert all(PASSWORD not in text for text in cursor.executed)
    assert connection.kwargs["autocommit"] is True
    assert (connection.kwargs["sslmode"], connection.kwargs["sslrootcert"]) == ("verify-full", CA_PATH)
    assert "3 login(s) created" in out.getvalue()


def test_existing_logins_keep_rotation_owned_passwords(provisioning):
    cursor, _ = provisioning(
        existing={"openfga", "openfga_runtime", "openfga_admin", "openfga_migrator"},
        usernames={"runtime": "openfga_runtime_clone"},
    )
    call_command("provision_openfga_database", stdout=StringIO())
    assert not any(text.startswith(("CREATE ROLE", "ALTER ROLE", "CREATE DATABASE")) for text in cursor.executed)


@pytest.mark.parametrize(
    "usernames",
    [{"runtime": "openfga_runtime_clone"}, {"admin": "someone_else"}],
)
def test_refuses_missing_base_after_rotation_or_foreign_login(provisioning, usernames):
    provisioning(existing={"openfga"}, usernames=usernames)
    with pytest.raises(CommandError) as raised:
        call_command("provision_openfga_database", stdout=StringIO())
    assert PASSWORD not in str(raised.value)


def test_requires_aws_and_closed_references(provisioning, monkeypatch):
    provisioning(existing=set())
    monkeypatch.setenv("OPENFGA_DATASTORE_LOGIN_ARNS", json.dumps({"runtime": ARNS["runtime"]}))
    with pytest.raises(CommandError):
        call_command("provision_openfga_database", stdout=StringIO())
    monkeypatch.setenv("OPENFGA_DATASTORE_LOGIN_ARNS", json.dumps(ARNS))
    monkeypatch.setenv("CLOUD_PROVIDER", "gcp")
    with pytest.raises(CommandError):
        call_command("provision_openfga_database", stdout=StringIO())


def test_runtime_iam_logins_are_created_once_with_rds_iam_and_existing_duty(monkeypatch):
    module = "shared.management.commands.provision_aws_runtime_logins"
    users = {
        "portal": "portal_web",
        "workers": "portal_workers",
        "ctf-scheduler": "ctf_scheduler",
        "provisioner-launcher": "provisioner_launcher",
    }
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("DB_SECRET_ID", "master-arn")
    monkeypatch.setenv("DB_SSLMODE", "verify-full")
    monkeypatch.setenv("DB_SSL_ROOT_CERT", CA_PATH)
    monkeypatch.setenv("AWS_DATABASE_IAM_USERS_JSON", json.dumps(users))
    master = {"host": "db.example.test", "port": 5432, "username": "master", "password": "m", "dbname": "shifter"}
    monkeypatch.setattr(
        f"{module}.get_secrets_store", lambda: type("S", (), {"get_secret": lambda self, ref: json.dumps(master)})()
    )
    cursor = _Cursor({"rds_iam", "portal_runtime", "portal_web"})

    class _Context(_Connection):
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    connected: dict[str, object] = {}
    monkeypatch.setattr(f"{module}.psycopg.connect", lambda **kwargs: connected.update(kwargs) or _Context(cursor))
    call_command("provision_aws_runtime_logins", stdout=StringIO())
    assert (connected["sslmode"], connected["sslrootcert"]) == ("verify-full", CA_PATH)
    created = [text for text in cursor.executed if text.startswith("CREATE ROLE")]
    assert created == [f'CREATE ROLE "{login}" LOGIN' for login in list(users.values())[1:]]
    assert sum(text.startswith("GRANT rds_iam TO") for text in cursor.executed) == 4
    assert sum(text.startswith("GRANT portal_runtime TO") for text in cursor.executed) == 4
    monkeypatch.setenv("AWS_DATABASE_IAM_USERS_JSON", json.dumps({**users, "portal": "portal_runtime"}))
    with pytest.raises(CommandError):
        call_command("provision_aws_runtime_logins", stdout=StringIO())


@pytest.mark.parametrize(
    ("module", "command"),
    [
        ("shared.management.commands.provision_openfga_database", "provision_openfga_database"),
        ("shared.management.commands.provision_aws_runtime_logins", "provision_aws_runtime_logins"),
        ("mission_control.management.commands.provision_guacamole_database", "provision_guacamole_database"),
    ],
)
@pytest.mark.parametrize(
    "tls", [{"DB_SSLMODE": "require"}, {"DB_SSL_ROOT_CERT": None, "DB_SSL_ROOT_CERT_PEM_B64": "e30="}]
)
def test_master_password_never_leaves_without_verified_tls(provisioning, monkeypatch, module, command, tls):
    provisioning(existing=set())
    master = {"host": "db.example.test", "port": 5432, "username": "master", "password": "m", "dbname": "shifter"}
    monkeypatch.setenv("AWS_DATABASE_IAM_USERS_JSON", json.dumps({"portal": "portal_web"}))
    monkeypatch.setenv("GUACAMOLE_DB_SECRET_ID", "master-arn")
    monkeypatch.setattr(
        f"{module}.get_secrets_store",
        lambda: type("S", (), {"get_secret": lambda self, ref: json.dumps({**master, "dbname": "guacamole"})})(),
    )
    for name, value in tls.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    attempts: list[dict[str, object]] = []
    monkeypatch.setattr(f"{module}.psycopg.connect", lambda **kwargs: attempts.append(kwargs))
    with pytest.raises(CommandError):
        call_command(command, stdout=StringIO())
    assert attempts == []
