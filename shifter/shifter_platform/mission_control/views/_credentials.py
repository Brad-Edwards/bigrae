"""Credential management views (HTML pages + JSON API)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.http import JsonResponse

from cms.services import (
    create_credential as cms_create_credential,
)
from shared.errors import classify_user_message
from shared.exceptions import CMSError

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from shared.schemas import CredentialRef, DeploymentProfileSpec, SCMCredentialSpec

    CredentialSpec = SCMCredentialSpec | DeploymentProfileSpec

logger = logging.getLogger(__name__)


class _CredentialError(Exception):
    """Internal exception carrying a JsonResponse for early-return guards."""

    def __init__(self, response: JsonResponse) -> None:
        super().__init__()
        self.response = response


def _validate_credential_spec(data: dict[str, Any], credential_type_slug: str) -> CredentialSpec:
    """Validate the create-credential payload via the matching Pydantic spec."""
    from pydantic import ValidationError as PydanticValidationError

    from shared.schemas import DeploymentProfileSpec, SCMCredentialSpec

    spec_class = SCMCredentialSpec if credential_type_slug == "scm" else DeploymentProfileSpec
    try:
        return spec_class.model_validate(data)
    except PydanticValidationError as e:
        errors = e.errors()
        raise _CredentialError(
            JsonResponse(
                {"error": errors[0]["msg"] if errors else "Validation failed"},
                status=400,
            )
        ) from e


def _persist_credential(user: User, credential_type_slug: str, kwargs: dict[str, Any]) -> CredentialRef:
    """Create the credential record, mapping service errors to ``_CredentialError``."""
    try:
        return cms_create_credential(user, credential_type_slug, **kwargs)
    except (CMSError, ValueError) as e:
        logger.exception("Credential creation failed: user=%s type=%s", user.pk, credential_type_slug)
        raise _CredentialError(
            JsonResponse({"error": classify_user_message(str(e), default="Invalid credential data")}, status=400)
        ) from e
    except ValidationError as e:
        raw = e.message_dict.get("__all__", [str(e)])[0] if hasattr(e, "message_dict") else str(e)
        # Use authored literals — never the original message — for the response body.
        if "unique_active_credential_name_per_user" in raw:
            response_msg = "A credential with this name already exists"
        else:
            response_msg = "Invalid credential data"
        logger.exception("Credential validation failed: user=%s type=%s", user.pk, credential_type_slug)
        raise _CredentialError(JsonResponse({"error": response_msg}, status=400)) from e
