"""Admission fence shared by every writer of a pack reference (ADR-034-R13, #2520).

A tenant pack removal locks its ``RaesPackageSource`` row exclusively, checks
for references and deletes the row in one transaction. A count-then-delete is
not enough on its own: references created concurrently would not be seen. Every
writer that creates or changes a reference therefore calls
:func:`fence_pack_reference` inside the transaction that commits it.

The fence takes ``FOR SHARE`` on PostgreSQL, so independent launches never
exclude each other, while removal (``FOR UPDATE NOWAIT``) and revision writes
(``FOR UPDATE``) exclude every in-flight reference writer. Writers take the
fence after their own workspace/event locks; removal never waits on it.
"""

from __future__ import annotations

from django.db import connection

from cms.exceptions import CMSError
from cms.models import RaesPackageSource, TenantPackRemoval


def fence_pack_reference(
    scenario_id: str, *, expected_digest: str | None = None, required: bool = True
) -> RaesPackageSource | None:
    """Share-lock a registered pack for the caller's transaction and return it.

    Launch writers pass ``required=True``: a launch needs a registered pack.
    CTF event and schedule writers pass ``required=False`` because an event may
    name a scenario that is not (yet) registered; they still get no row back for
    such an id, but a pack that a removal deleted is always refused.

    Raises:
        CMSError: when no transaction is open, a required pack is not registered,
            the pack was removed, or its digest no longer matches the caller's.
    """
    if not connection.in_atomic_block:
        raise CMSError("A pack reference must be admitted inside a transaction")
    queryset = RaesPackageSource.objects.filter(scenario_id=scenario_id)
    if connection.vendor == "postgresql":
        # Django has no FOR SHARE queryset operation; SQL comes from the ORM
        # compiler, as in engine.services._model_allocation_authority.
        sql, params = queryset.values_list("pk", flat=True).query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(sql + " FOR SHARE", params)  # NOSONAR -- SQL and params come from Django's ORM compiler.
    source = queryset.first()
    if source is None:
        if TenantPackRemoval.objects.filter(scenario_id=scenario_id).exists():
            raise CMSError(f"Scenario '{scenario_id}' was removed")
        if required:
            raise CMSError(f"Scenario '{scenario_id}' is not registered")
        return None
    if expected_digest is not None and source.package_digest != expected_digest:
        raise CMSError("The pack changed during admission; retry with its current version")
    return source
