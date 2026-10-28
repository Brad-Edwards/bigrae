"""NGFW management views (HTML pages + JSON API)."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth.models import User
from django.http import Http404, JsonResponse

from cms.services import (
    destroy_ngfw as cms_destroy_ngfw,
)
from shared.errors import classify_user_message
from shared.exceptions import CMSError
from shared.log_sanitize import safe_log_value
from shared.schemas.app import NGFWOneTimeRegistration

from ._common import (
    NGFW_NOT_FOUND,
)

logger = logging.getLogger(__name__)


class _NgfwError(Exception):
    """Internal exception carrying a JsonResponse for early-return guards."""

    def __init__(self, response: JsonResponse) -> None:
        super().__init__()
        self.response = response


def _extract_ngfw_create_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise the api_ngfw_create payload (string IDs → ints, strip OTP fields)."""
    deployment_profile_id = data.get("deployment_profile_id")
    scm_credential_id = data.get("scm_credential_id")
    if deployment_profile_id:
        deployment_profile_id = int(deployment_profile_id)
    if scm_credential_id:
        scm_credential_id = int(scm_credential_id)
    return {
        "name": data.get("name", "").strip(),
        "deployment_profile_id": deployment_profile_id,
        "registration_method": data.get("registration_method"),
        "scm_credential_id": scm_credential_id,
        "otp": NGFWOneTimeRegistration(
            data.get("otp_value", "").strip() if data.get("otp_value") else None,
            data.get("otp_folder", "").strip() if data.get("otp_folder") else None,
        ),
    }


def _run_ngfw_destroy(user: User, app_id: str, confirm_name: str) -> None:
    """Invoke ``cms_destroy_ngfw`` and translate errors to ``_NgfwError`` or Http404."""
    try:
        cms_destroy_ngfw(user, app_id, confirm_name)
    except CMSError as e:
        if "not found" in str(e).lower():
            raise Http404(NGFW_NOT_FOUND) from None
        logger.exception("NGFW destroy failed (CMSError): user=%s app_id=%s", user.pk, safe_log_value(app_id))
        raise _NgfwError(
            JsonResponse({"error": classify_user_message(str(e), default="NGFW could not be destroyed")}, status=400)
        ) from e
    except ValueError as e:
        logger.exception("NGFW destroy failed (ValueError): user=%s app_id=%s", user.pk, safe_log_value(app_id))
        raise _NgfwError(
            JsonResponse({"error": classify_user_message(str(e), default="Invalid destroy request")}, status=400)
        ) from e
