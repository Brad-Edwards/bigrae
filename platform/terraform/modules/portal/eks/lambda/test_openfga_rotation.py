"""Behavior tests for the OpenFGA PostgreSQL rotation Lambda (#2313)."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

import openfga_rotation

MASTER_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/database"
SECRET_ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-runtime-datastore"
PASSWORD_MARKER = "SyntheticPendingPassword2313"
# The alternating master secret is the portal RDS credential, copied unchanged.
RDS_PRODUCER = Path(__file__).resolve().parents[2] / "rds" / "main.tf"


class _NotFound(Exception):
    pass


class _SecretsManager:
    """A small in-memory Secrets Manager with version stages."""

    exceptions = type("Exceptions", (), {"ResourceNotFoundException": _NotFound})

    def __init__(self, current: dict[str, Any], master: dict[str, Any]) -> None:
        self.values = {SECRET_ARN: {"v1": current}, MASTER_ARN: {"m1": master}}
        self.stages = {SECRET_ARN: {"v1": ["AWSCURRENT"]}, MASTER_ARN: {"m1": ["AWSCURRENT"]}}
        self.rotation_enabled = True

    def describe_secret(self, SecretId: str) -> dict[str, Any]:
        return {"RotationEnabled": self.rotation_enabled, "VersionIdsToStages": self.stages[SecretId]}

    def rotate_secret(self, token: str) -> None:
        """RotateSecret registers the version as AWSPENDING before createSecret."""
        self.stages[SECRET_ARN][token] = ["AWSPENDING"]

    def get_secret_value(self, SecretId: str, VersionStage: str, VersionId: str | None = None) -> dict[str, Any]:
        for version, labels in self.stages[SecretId].items():
            has_value = version in self.values[SecretId]
            if has_value and VersionStage in labels and (VersionId is None or VersionId == version):
                return {"SecretString": json.dumps(self.values[SecretId][version])}
        raise _NotFound()

    def get_random_password(self, **kwargs: Any) -> dict[str, str]:
        assert kwargs["PasswordLength"] == 32
        return {"RandomPassword": PASSWORD_MARKER}

    def put_secret_value(self, SecretId: str, ClientRequestToken: str, SecretString: str, VersionStages: list[str]):
        self.values[SecretId][ClientRequestToken] = json.loads(SecretString)
        self.stages[SecretId][ClientRequestToken] = list(VersionStages)

    def update_secret_version_stage(self, SecretId, VersionStage, MoveToVersionId, RemoveFromVersionId):
        self.stages[SecretId][RemoveFromVersionId].remove(VersionStage)
        self.stages[SecretId][MoveToVersionId].append(VersionStage)


class _Database:
    """Record SQL per login; existing roles and failing logins are configurable."""

    def __init__(self) -> None:
        self.roles = {"openfga_runtime"}
        self.statements: list[tuple[str, str]] = []
        self.connections: list[dict[str, Any]] = []
        self.refused: set[str] = set()

    @contextmanager
    def connect(self, **kwargs: Any):
        self.connections.append(kwargs)
        if kwargs["password"] in self.refused:
            raise psycopg.OperationalError("synthetic authentication failure")
        database = self

        class _PgConn:
            def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes) -> bytes:
                assert algorithm == b"scram-sha-256"
                return b"SCRAM-SHA-256$4096:synthetic-salt$synthetic-stored:synthetic-server"

        class _Cursor:
            def __init__(self) -> None:
                self._row = None

            def execute(self, query: Any, params: list[str] | None = None) -> None:
                text = query if isinstance(query, str) else query.as_string(None)
                database.statements.append((kwargs["username"], text))
                if text.startswith("SELECT 1 FROM pg_roles"):
                    self._row = (1,) if params and params[0] in database.roles else None
                if text.startswith("CREATE ROLE"):
                    database.roles.add(text.split('"')[1])

            def fetchone(self):
                return self._row

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class _Connection:
            pgconn = _PgConn()

            def cursor(self):
                return _Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        yield _Connection()


def _credential(username: str, password: str, **extra: Any) -> dict[str, Any]:
    return {
        "engine": "postgres",
        "host": "db.example.test",
        "port": 5432,
        "dbname": "openfga",
        "username": username,
        "password": password,
        **extra,
    }


def _producer_master() -> dict[str, Any]:
    """Return a master credential with the engine value the RDS module writes."""
    block = RDS_PRODUCER.read_text(encoding="utf-8").split('"aws_secretsmanager_secret_version" "db_credentials"')[1]
    engine = re.search(r'^\s*engine\s*=\s*"([^"]+)"', block, re.MULTILINE)
    assert engine is not None
    return {**_credential("master", "synthetic-master"), "engine": engine.group(1)}


@pytest.fixture
def rotation(monkeypatch):
    def build(strategy: str, current: dict[str, Any], assigned: str | None = None):
        secrets = _SecretsManager(current, _producer_master())
        database = _Database()
        monkeypatch.setenv("ROTATION_STRATEGY", strategy)
        # Terraform binds each secret ARN to its base login and names the master secret.
        base = assigned or current["username"].removesuffix("_clone")
        monkeypatch.setenv("ROTATION_LOGINS", json.dumps({SECRET_ARN: base}))
        monkeypatch.setenv("MASTER_SECRET_ARN", MASTER_ARN)
        monkeypatch.setattr(openfga_rotation, "_client", lambda: secrets)
        monkeypatch.setattr(openfga_rotation, "_connect", lambda credential: database.connect(**credential))
        return secrets, database

    return build


def _run(token: str) -> None:
    openfga_rotation._client().rotate_secret(token)
    for step in ("createSecret", "setSecret", "testSecret", "finishSecret"):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": token, "Step": step}, None)


def test_alternating_rotation_creates_clone_once_then_alternates(rotation):
    secrets, database = rotation(
        "alternating", _credential("openfga_runtime", "synthetic-current", masterarn=MASTER_ARN)
    )
    _run("v2")
    assert secrets.values[SECRET_ARN]["v2"]["username"] == "openfga_runtime_clone"
    assert secrets.stages[SECRET_ARN]["v2"] == ["AWSPENDING", "AWSCURRENT"]
    master_sql = [text for user, text in database.statements if user == "master"]
    assert any(text.startswith('CREATE ROLE "openfga_runtime_clone" WITH LOGIN PASSWORD') for text in master_sql)
    assert 'GRANT "openfga_runtime" TO "openfga_runtime_clone"' in master_sql
    assert all(PASSWORD_MARKER not in text for _, text in database.statements)
    database.statements.clear()
    _run("v3")
    assert secrets.values[SECRET_ARN]["v3"]["username"] == "openfga_runtime"
    altered = [text for user, text in database.statements if user == "master" and text.startswith("ALTER ROLE")]
    assert altered == [
        'ALTER ROLE "openfga_runtime" WITH PASSWORD '
        + "'SCRAM-SHA-256$4096:synthetic-salt$synthetic-stored:synthetic-server'"
    ]


def test_single_user_rotation_changes_only_its_own_password(rotation):
    secrets, database = rotation("single", _credential("openfga_migrator", "synthetic-current"))
    _run("v2")
    assert secrets.values[SECRET_ARN]["v2"]["username"] == "openfga_migrator"
    assert [user for user, text in database.statements if text.startswith("ALTER ROLE")] == ["openfga_migrator"]
    assert all(PASSWORD_MARKER not in text for _, text in database.statements)


def test_single_user_retry_after_lost_acknowledgement_uses_pending_credential(rotation):
    secrets, database = rotation("single", _credential("openfga_migrator", "synthetic-current"))
    secrets.rotate_secret("v2")
    openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    database.refused.add("synthetic-current")
    openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "setSecret"}, None)
    assert database.connections[-1]["password"] == PASSWORD_MARKER


def test_refuses_target_changes_disabled_rotation_and_unstaged_versions(rotation):
    secrets, _ = rotation(
        "alternating", _credential("openfga_runtime", "synthetic-current", masterarn=MASTER_ARN)
    )
    secrets.rotate_secret("v2")
    openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    secrets.values[SECRET_ARN]["v2"]["host"] = "attacker.example.test"
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "setSecret"}, None)
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v9", "Step": "setSecret"}, None)
    secrets.rotation_enabled = False
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "setSecret"}, None)


def test_secret_content_cannot_redirect_master_authority_to_another_login(rotation):
    # A writer of the runtime secret renames its login to the admin clone.
    secrets, database = rotation(
        "alternating",
        _credential("openfga_admin_clone", "synthetic-current", masterarn=MASTER_ARN),
        assigned="openfga_runtime",
    )
    secrets.rotate_secret("v2")
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    assert "v2" not in secrets.values[SECRET_ARN]
    assert database.statements == []
    # A pending version edited to another login is refused before the master connects.
    secrets, database = rotation("alternating", _credential("openfga_runtime", "synthetic-current"))
    secrets.rotate_secret("v2")
    openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    secrets.values[SECRET_ARN]["v2"]["username"] = "openfga_admin"
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "setSecret"}, None)
    assert database.statements == []


def test_configured_master_wins_over_the_secret_masterarn(rotation):
    _, database = rotation(
        "alternating",
        _credential(
            "openfga_runtime",
            "synthetic-current",
            masterarn="arn:aws:secretsmanager:us-east-2:123456789012:secret:other",
        ),
    )
    _run("v2")
    assert any(user == "master" for user, _ in database.statements)


def test_unassigned_secret_fails_closed(rotation, monkeypatch):
    secrets, database = rotation("single", _credential("openfga_migrator", "synthetic-current"))
    monkeypatch.setenv("ROTATION_LOGINS", json.dumps({}))
    secrets.rotate_secret("v2")
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    assert database.statements == []


def test_driver_errors_are_bounded_without_secret_detail(rotation, caplog):
    secrets, database = rotation(
        "alternating", _credential("openfga_runtime", "synthetic-current", masterarn=MASTER_ARN)
    )
    secrets.rotate_secret("v2")
    openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)
    database.refused.add(PASSWORD_MARKER)
    with pytest.raises(openfga_rotation.RotationError) as raised:
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "testSecret"}, None)
    assert raised.value.__cause__ is None
    assert PASSWORD_MARKER not in caplog.text
    assert "synthetic authentication failure" not in caplog.text


def test_missing_strategy_fails_closed(rotation, monkeypatch):
    secrets, _ = rotation("alternating", _credential("openfga_runtime", "synthetic-current", masterarn=MASTER_ARN))
    secrets.rotate_secret("v2")
    monkeypatch.delenv("ROTATION_STRATEGY")
    with pytest.raises(openfga_rotation.RotationError):
        openfga_rotation.handler({"SecretId": SECRET_ARN, "ClientRequestToken": "v2", "Step": "createSecret"}, None)


def test_connections_verify_server_certificate_and_host(monkeypatch):
    seen = {}
    monkeypatch.setattr(openfga_rotation.psycopg, "connect", lambda **kwargs: seen.update(kwargs) or object())
    openfga_rotation._connect(_credential("openfga_runtime", "synthetic-current"))
    assert seen["sslmode"] == "verify-full"
    assert seen["sslrootcert"].endswith("rds-ca-bundle.pem")
    assert seen["connect_timeout"] == 5
