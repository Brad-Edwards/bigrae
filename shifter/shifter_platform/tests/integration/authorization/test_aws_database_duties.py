"""AWS per-workload RDS IAM logins and policy duties on real PostgreSQL (#2313)."""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from shared.aws_database_duties import OPENFGA_LOGINS

pytestmark = [pytest.mark.openfga, pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

USERS = {
    "portal": "synthetic_portal_web",
    "workers": "synthetic_portal_workers",
    "ctf-scheduler": "synthetic_ctf_scheduler",
    "provisioner-launcher": "synthetic_provisioner_launcher",
}


def test_owner_creates_iam_logins_that_inherit_only_the_runtime_duty(monkeypatch):
    """Use PostgreSQL's real role inheritance and table ACL enforcement."""
    import psycopg

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_iam'")
        if cursor.fetchone() is None:
            cursor.execute("CREATE ROLE rds_iam NOLOGIN")
        # The provisioning Job creates the policy database and its password logins first.
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = 'openfga'")
        assert cursor.fetchone() is not None, "the released OpenFGA harness provides the policy database"
        for login in OPENFGA_LOGINS.values():
            cursor.execute(f'CREATE ROLE "{login}" LOGIN')
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("DB_AUTH_MODE", "password")
    monkeypatch.delenv("DB_IAM_AUTH", raising=False)
    monkeypatch.setenv("AWS_DATABASE_IAM_USERS_JSON", json.dumps(USERS))
    try:
        call_command("reconcile_aws_database_duties")
        call_command("reconcile_aws_database_duties")
        with connection.cursor() as cursor:
            for login in USERS.values():
                cursor.execute("SELECT rolcanlogin, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=%s", [login])
                assert cursor.fetchone() == (True, False, False)
                cursor.execute(
                    "SELECT pg_has_role(%s, 'rds_iam', 'MEMBER'), pg_has_role(%s, 'portal_runtime', 'MEMBER')",
                    [login, login],
                )
                assert cursor.fetchone() == (True, True)
                cursor.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", [login])
                assert cursor.fetchone() == (False,)
                cursor.execute("SELECT has_table_privilege(%s, 'django_migrations', 'UPDATE')", [login])
                assert cursor.fetchone() == (True,)
                cursor.execute("SELECT rolpassword IS NULL FROM pg_authid WHERE rolname=%s", [login])
                assert cursor.fetchone() == (True,)
            cursor.execute("SELECT current_database()")
            application_database = cursor.fetchone()[0]
            for login in OPENFGA_LOGINS.values():
                cursor.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')", [login, application_database])
                assert cursor.fetchone() == (False,)
        with (
            psycopg.connect(**{**connection.get_connection_params(), "dbname": "openfga"}) as policy,
            policy.cursor() as cursor,
        ):
            migrator = OPENFGA_LOGINS["openfga-migrator"]
            cursor.execute("SELECT has_table_privilege(%s, 'goose_db_version', 'UPDATE')", [migrator])
            assert cursor.fetchone() == (True,)
            cursor.execute("SELECT rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=%s", [migrator])
            assert cursor.fetchone() == (False, False)
    finally:
        with connection.cursor() as cursor:
            for login in [*USERS.values(), *OPENFGA_LOGINS.values()]:
                cursor.execute(f'DROP OWNED BY "{login}"')
                cursor.execute(f'DROP ROLE IF EXISTS "{login}"')


def test_runtime_iam_login_cannot_administer_duties(monkeypatch):
    monkeypatch.setenv("CLOUD_PROVIDER", "aws")
    monkeypatch.setenv("DB_AUTH_MODE", "rds-iam")
    monkeypatch.setenv("AWS_DATABASE_IAM_USERS_JSON", json.dumps(USERS))
    with pytest.raises(CommandError):
        call_command("reconcile_aws_database_duties")
