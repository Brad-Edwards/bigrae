"""Canonical upload storage namespace derived from exact native scope."""

from shared.identity_scope import ResourceScope
from shared.operation_envelope import canonical_payload_digest

from ._content_authority import content_scope_fields


def _scope_digest(scope: ResourceScope) -> str:
    """Derive the storage budget identity from resolved content ancestry."""
    return canonical_payload_digest(
        {name: str(value) if value is not None else None for name, value in content_scope_fields(scope).items()}
    )
