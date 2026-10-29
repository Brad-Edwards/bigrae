"""SQL-only custody fences for previously admitted remote session delivery."""

from django.db import transaction

from shared.operation_authority import authority_resource_scope
from workspaces.services import resolve_range_scope_binding

from ._range_access import assert_range_remote_authority_binding
from ._scoped_devices import assert_device_remote_authority_binding


def assert_remote_authority_binding(authority: dict[str, object], protocol: str, target: str) -> None:
    """Consume trusted recorded facts without granting or rechecking provider policy."""
    if not transaction.get_connection().in_atomic_block or authority.get("authority_kind") != "principal":
        raise ValueError("Remote binding unavailable")
    scope = authority_resource_scope(authority)
    resolve_range_scope_binding(scope, for_update=True)
    if protocol == "ngfw_ssh":
        assert_device_remote_authority_binding(authority, target)
    elif protocol in {"range_ssh", "rdp"}:
        assert_range_remote_authority_binding(authority, target)
    else:
        raise ValueError("Remote binding unavailable")
