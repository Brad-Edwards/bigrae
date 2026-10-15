"""Validate credential secrets through the incumbent closed specifications."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from cms.exceptions import CMSError
from shared.schemas.registry import get_model_for_slug


def validate_credential_data(
    spec_slug: str,
    data: dict[str, Any],
    *,
    name: str,
    expires_at: datetime | None,
    user_id: int | None,
    owner_principal_uuid: UUID | None = None,
) -> dict[str, Any]:
    """Return only type-specific values; never expose secret validation details."""
    try:
        specification = get_model_for_slug(spec_slug).model_validate(
            {
                **data,
                "name": name,
                "expires_at": expires_at,
                "user_id": user_id,
                "owner_principal_uuid": owner_principal_uuid,
            }
        )
    except (ValidationError, LookupError):
        raise CMSError("Invalid credential input") from None
    return specification.model_dump(exclude={"name", "expires_at", "user_id", "owner_principal_uuid", "uuid"})
