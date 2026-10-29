"""Native principal entrypoints for encrypted credential custody."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from shared.identity_scope import ResourceScope

from ._content_authority import require_native_content_credential

if TYPE_CHECKING:
    from shared.schemas.credentials import CredentialContext as CredentialProjection
    from shared.schemas.credentials import CredentialRef


def create_credential(user: object, credential_type_slug: str, **kwargs: object) -> CredentialRef:
    """Create encrypted credentials in an explicitly authorized native scope."""
    from ._scoped_credentials import create_scoped_credential

    return create_scoped_credential(
        require_native_content_credential(user),
        credential_type_slug,
        cast(ResourceScope, kwargs.pop("scope", None)),
        kwargs,
    )


def delete_credential(user: object, credential_id: int) -> CredentialRef:
    """Delete the exact credential under current native custody."""
    from ._scoped_credentials import delete_scoped_credential

    return delete_scoped_credential(require_native_content_credential(user), credential_id)


def list_credentials(user: object, *, scope: ResourceScope | None = None) -> list[CredentialProjection]:
    """List safe projections for an authorized native scope."""
    from ._scoped_credentials import list_scoped_credentials

    return list_scoped_credentials(require_native_content_credential(user), scope)


def get_credential(user: object, credential_id: int) -> CredentialProjection:
    """Read one safe projection under current native custody."""
    from ._scoped_credentials import get_scoped_credential

    return get_scoped_credential(require_native_content_credential(user), credential_id)
