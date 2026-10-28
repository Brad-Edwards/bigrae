"""Historical-schema proof for the audit integrity-chain backfill (#327).

Each migration round trip costs time on PostgreSQL, so the chain backfill and
the legacy-sequence grant share one round trip (#2582).
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from shared.audit.integrity import digest_for_row
from shared.models import AuditChainHead

BEFORE = [("shared", "0019_alter_auditlog_entity_type")]
AFTER = [("shared", "0020_audit_integrity_chain")]

pytestmark = [pytest.mark.django_db(transaction=True)]


def _migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


@pytest.fixture
def historical_audit_schema():
    apps = _migrate(BEFORE)
    try:
        yield apps
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_upgrade_backfills_one_verifiable_chain_and_grants_the_legacy_sequence(historical_audit_schema):
    """The upgrade chains every legacy row; on PostgreSQL it also grants a renamed legacy sequence."""
    postgres = connection.vendor == "postgresql"
    legacy_sequence = "risk_register_auditlog_id_seq"
    if postgres:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER SEQUENCE public.shared_auditlog_id_seq RENAME TO {legacy_sequence}")

    try:
        legacy = historical_audit_schema.get_model("shared", "AuditLog")
        first = legacy.objects.create(
            entity_type="retired_entity",
            entity_id=71,
            action="retired_action",
            actor_type="system",
            context="first legacy row",
        )
        second = legacy.objects.create(
            entity_type="range",
            entity_id=72,
            action="update",
            actor_type="system",
            context="second legacy row",
        )

        after_apps = _migrate(AFTER)

        # Query the historical model, whose table predates newer actor columns.
        rows = list(after_apps.get_model("shared", "AuditLog").objects.order_by("sequence"))
        assert [row.pk for row in rows] == [first.pk, second.pk]
        assert [row.sequence for row in rows] == [1, 2]
        assert rows[0].previous_digest == ""
        assert rows[1].previous_digest == rows[0].record_digest
        assert [digest_for_row(row) for row in rows] == [row.record_digest for row in rows]
        head = AuditChainHead.objects.get(singleton=1)
        assert head.last_sequence == 2
        assert head.last_digest == rows[-1].record_digest

        if postgres:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_get_serial_sequence('public.shared_auditlog', 'id')")
                owned_sequence = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT has_sequence_privilege('portal_runtime', %s, 'USAGE')",
                    [owned_sequence],
                )
                has_usage = bool(cursor.fetchone()[0])
            assert owned_sequence.endswith(legacy_sequence)
            assert has_usage
    finally:
        if postgres:
            with connection.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.risk_register_auditlog_id_seq')")
                if cursor.fetchone()[0] is not None:
                    cursor.execute(f"ALTER SEQUENCE public.{legacy_sequence} RENAME TO shared_auditlog_id_seq")
