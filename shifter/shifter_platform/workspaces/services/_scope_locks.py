"""Canonical ancestry lock order for commands spanning one or more resource scopes."""

from collections.abc import Iterable

from django.db import transaction

from shared.identity_scope import ResourceScope

from ._account import AccountScopeError, ResolvedResourceScope, resolve_range_scope_binding


def lock_resource_scope_bindings(scopes: Iterable[ResourceScope]) -> dict[ResourceScope, ResolvedResourceScope]:
    """Lock all parents before domain resources, ordered across source/destination accounts."""
    if not transaction.get_connection().in_atomic_block:
        raise AccountScopeError("Scope locks require a transaction")
    ordered = sorted(
        set(scopes),
        key=lambda scope: tuple(
            str(value or "")
            for value in (scope.kind, scope.account_uuid, scope.organization_uuid, scope.workspace_uuid)
        ),
    )
    return {scope: resolve_range_scope_binding(scope, for_update=True) for scope in ordered}
