"""Create the OpenFGA database and its password logins on the shared RDS (#2313).

The provisioning Job runs this before the chart, as the RDS master. It creates
the dedicated ``openfga`` database and each duty's base login from its current
Secrets Manager secret. An existing login keeps its password: the rotation
function owns it. The owner reconciliation later transfers database ownership
and grants the duties. Passwords reach the server only as SCRAM verifiers.
"""

from __future__ import annotations

import json
import os
from typing import Any

import psycopg
from django.core.management.base import BaseCommand, CommandError
from psycopg import sql

from shared.aws_database_duties import OPENFGA_LOGINS, ensure_password_login, master_connection_parameters
from shared.cloud import get_secrets_store

_DATABASE = "openfga"
_DUTY_SECRETS = {"runtime": "openfga-runtime", "admin": "openfga-admin", "migrator": "openfga-migrator"}


class Command(BaseCommand):
    """Idempotently create the OpenFGA database and base logins."""

    help = "Create the OpenFGA database and its datastore logins on the shared RDS instance."

    def handle(self, *args: object, **options: object) -> None:
        """Create the OpenFGA database and base logins on AWS; refuse any other provider."""
        if os.environ.get("CLOUD_PROVIDER") != "aws":
            raise CommandError("OpenFGA database provisioning requires the AWS deployment")
        master_secret = os.environ.get("DB_SECRET_ID", "")
        try:
            references = json.loads(os.environ.get("OPENFGA_DATASTORE_LOGIN_ARNS", ""))
            if not master_secret or not isinstance(references, dict) or set(references) != set(_DUTY_SECRETS):
                raise ValueError
            store = get_secrets_store()
            master = json.loads(store.get_secret(master_secret))
            credentials = {duty: json.loads(store.get_secret(references[duty])) for duty in _DUTY_SECRETS}
            created = self._provision(master, credentials)
        except Exception:
            # Driver and provider errors can carry connection or credential detail.
            raise CommandError(
                "OpenFGA database provisioning failed; verify the master and datastore secrets"
            ) from None
        self.stdout.write(f"OpenFGA database is provisioned; {created} login(s) created.")

    @staticmethod
    def _provision(master: dict[str, Any], credentials: dict[str, dict[str, Any]]) -> int:
        connection = psycopg.connect(**master_connection_parameters(master), autocommit=True)
        created = 0
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", [_DATABASE])
                if cursor.fetchone() is None:
                    # CREATE DATABASE cannot run in a transaction; autocommit is set.
                    cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_DATABASE)))
                for duty, workload in _DUTY_SECRETS.items():
                    if ensure_password_login(cursor, connection.pgconn, credentials[duty], OPENFGA_LOGINS[workload]):
                        created += 1
        finally:
            connection.close()
        return created
