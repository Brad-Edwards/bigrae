"""Guacamole bootstrap polling views."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.http import JsonResponse
from django.urls import reverse

from mission_control.models import GuacamoleBootstrapRequest

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)


class _BootstrapViewError(Exception):
    """Internal exception carrying a pre-built JsonResponse for early return."""

    def __init__(self, response: JsonResponse) -> None:
        super().__init__()
        self.response = response


def _authenticated_user_id(user: User) -> int:
    """Return the authenticated user's integer id."""
    for attr in ("pk", "id"):
        value = getattr(user, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    raise _BootstrapViewError(JsonResponse({"error": "Authenticated user id unavailable"}, status=500))


def _bootstrap_urls(
    request_id: UUID,
    *,
    status_url_name: str = "v1:mission_control:guacamole-bootstrap-status",
    open_url_name: str = "v1:mission_control:guacamole-bootstrap-open",
) -> tuple[str, str]:
    """Return the polling URL and compatibility opener URL for a bootstrap."""
    kwargs = {"request_id": request_id}
    status_url = reverse(status_url_name, kwargs=kwargs)
    open_url = reverse(open_url_name, kwargs=kwargs)
    return status_url, open_url


def _mark_expired(bootstrap: GuacamoleBootstrapRequest) -> None:
    """Persist expiry as a failed bootstrap when the work did not finish."""
    if bootstrap.status not in {
        GuacamoleBootstrapRequest.Status.PENDING,
        GuacamoleBootstrapRequest.Status.RUNNING,
    }:
        return
    bootstrap.status = GuacamoleBootstrapRequest.Status.FAILED
    bootstrap.error_message = "Guacamole session request expired"
    bootstrap.error_status_code = 410
    bootstrap.save(update_fields=("status", "error_message", "error_status_code", "updated_at"))
