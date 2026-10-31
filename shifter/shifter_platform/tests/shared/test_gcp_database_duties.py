"""SQL duty reconciliation before the first application migration (#2704)."""

from __future__ import annotations

from shared.gcp_native_identity.database_duties import reconcile_application_duties


class _Cursor:
    """Answer the schema check and record every statement."""

    def __init__(self, *, schema_present: bool) -> None:
        self.schema_present = schema_present
        self.statements: list[str] = []

    def execute(self, query, params=None, /):
        self.statements.append(str(query))

    def fetchone(self):
        return (self.schema_present,)

    def fetchall(self):
        return []


def test_a_database_without_the_application_schema_is_left_unchanged():
    cursor = _Cursor(schema_present=False)
    reconcile_application_duties(cursor, {"portal": "synthetic-portal@synthetic-project.iam"})
    assert cursor.statements == ["SELECT to_regclass('public.django_migrations') IS NOT NULL"]
