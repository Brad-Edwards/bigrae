"""Create the per-workload RDS IAM logins before the identity status probe (#2313).

The provisioning Job runs this as the RDS master, before the chart and before the
identity cutover probe, which connects as the portal login. Each login gets
``rds_iam`` and, when migrations created it earlier, the ``portal_runtime`` duty.
The migration Job later reconciles every duty under the schema owner.
"""

from __future__ import annotations

import json
import os

import psycopg
from django.core.management.base import BaseCommand, CommandError
from psycopg import sql

from shared.aws_database_duties import ensure_iam_logins, master_connection_parameters, parse_iam_users
from shared.cloud import get_secrets_store
from shared.cloud.exceptions import CloudSecretsError


class Command(BaseCommand):
    """Idempotently create the runtime RDS IAM logins."""

    help = "Create the per-workload RDS IAM logins on the shared RDS instance."

    def handle(self, *args: object, **options: object) -> None:
        """Create the runtime IAM logins on AWS; refuse any other provider."""
        if os.environ.get("CLOUD_PROVIDER") != "aws":
            raise CommandError("RDS IAM login provisioning requires the AWS deployment")
        try:
            users = parse_iam_users(os.environ.get("AWS_DATABASE_IAM_USERS_JSON", ""))
            master = json.loads(get_secrets_store().get_secret(os.environ["DB_SECRET_ID"]))
            with (
                psycopg.connect(**master_connection_parameters(master)) as connection,
                connection.cursor() as cursor,
            ):
                ensure_iam_logins(cursor, users)
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", ["portal_runtime"])
                if cursor.fetchone() is not None:
                    for login in users.values():
                        cursor.execute(sql.SQL("GRANT portal_runtime TO {}").format(sql.Identifier(login)))
        except (psycopg.Error, CloudSecretsError, OSError, ValueError, KeyError, TypeError):
            # Driver and provider errors can carry connection or credential detail.
            raise CommandError("RDS IAM login provisioning failed; verify the master secret and login map") from None
        self.stdout.write("RDS IAM runtime logins are provisioned.")
