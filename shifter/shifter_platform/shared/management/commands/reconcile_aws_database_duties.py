"""Deployment-owner AWS SQL duty bootstrap; runtime logins hold no password."""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction
from psycopg import Error as PostgresError

from shared.aws_database_duties import OPENFGA_LOGINS, ensure_iam_logins, parse_iam_users
from shared.gcp_native_identity.database_duties import reconcile_application_duties, reconcile_policy_owner


class Command(BaseCommand):
    """Create per-workload RDS IAM logins and reconcile duties under the owner."""

    help = "Reconcile AWS PostgreSQL duties after migrations under the RDS master owner."

    def handle(self, *args: object, **options: object) -> None:
        """Reconcile the AWS application and policy duties as the owner."""
        if connection.vendor != "postgresql" or os.environ.get("CLOUD_PROVIDER") != "aws":
            raise CommandError("AWS database duty reconciliation requires PostgreSQL and the AWS deployment owner")
        if os.environ.get("DB_AUTH_MODE", "password") != "password" or os.environ.get("DB_IAM_AUTH") == "true":
            raise CommandError("Runtime IAM database logins cannot administer database duties")
        try:
            users = parse_iam_users(os.environ.get("AWS_DATABASE_IAM_USERS_JSON", ""))
            with transaction.atomic(), connection.cursor() as cursor:
                ensure_iam_logins(cursor, users)
                reconcile_application_duties(cursor, users)
            reconcile_policy_owner(connection.get_connection_params(), {**users, **OPENFGA_LOGINS})
        except (ValueError, TypeError, DatabaseError, PostgresError):
            raise CommandError(
                "AWS SQL duty reconciliation failed; verify the owner's SQL authority and the provisioned logins"
            ) from None
        self.stdout.write("AWS database duties reconciled")
