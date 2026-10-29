"""Local native proof, scope and immutable reference fences for registration."""

from __future__ import annotations

from uuid import UUID

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

from ._pack_registration_custody import fence_registration_reference
from ._pack_registration_types import PackRegistrationAdmission, PackRegistrationRequest

_PACK_REGISTRATION_IS_UNAVAILABLE = "Pack registration is unavailable"


def _fence_registration_reference(
    user: CredentialContext,
    request: PackRegistrationRequest,
    scope: ResourceScope | None,
    prepared: PackRegistrationAdmission | None,
) -> None:
    """Recheck exact reference custody inside the registration transaction."""
    if prepared is None or scope is None:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    fence_registration_reference(user, request, scope, prepared.reference)


def _fence_registration_scope(user: CredentialContext, scope: ResourceScope | None) -> None:
    """Hold native proof and current ancestry through the registration write."""
    from ._content_authority import fence_content_scope

    if scope is None:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    fence_content_scope(user, scope)


def _fence_registration_credential(user: CredentialContext) -> None:
    """A native registration admission cannot survive local proof revocation."""
    from shared.credentials import resolve_local_credential

    from ._content_authority import ContentAuthorityError

    try:
        if resolve_local_credential(user) != user:
            raise ValueError("Registration proof changed")
    except ValueError:
        raise ContentAuthorityError(_PACK_REGISTRATION_IS_UNAVAILABLE) from None


def _registration_scope_matches(
    row: RaesPackageSource, organization_uuid: UUID | None, scope: ResourceScope | None
) -> bool:
    """Compare the registered source with its admitted content scope."""
    if scope is None:
        return not row.scope_kind and row.organization_uuid == organization_uuid
    from ._content_authority import content_scope_fields

    return row.owner_principal_uuid is not None and all(
        getattr(row, name) == value for name, value in content_scope_fields(scope).items()
    )
