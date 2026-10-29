"""Shared DRF boundary helpers for the CTF API."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from django.http import HttpResponse, JsonResponse
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.bridges import get_user_role
from ctf.services.participant import is_viewing_participant
from shared.api.errors import api_error_response
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import active_actor_user, authenticated_credential
from shared.api_tokens.models import ApiToken
from shared.api_tokens.scopes import has_scope
from shared.credentials import CredentialContext

PermissionClass = type[permissions.BasePermission]


def ctf_actor_user(request: Request) -> Any | None:
    """Return the active user represented by a session or platform API token."""
    return active_actor_user(request)


class _CtfApiError(Exception):
    """Internal control-flow error rendered as the shared CTF API error envelope.

    Boundary helpers (ownership resolution, body validation, service-exception
    mapping) raise this instead of returning ``(obj, error)`` tuples, so a
    canonical CTF view method needs only a single ``except _CtfApiError`` to
    render the exact legacy status code, error code, and message via
    :func:`api_error_response`. ``headers`` replays any response headers (e.g.
    ``Retry-After``) that the legacy path set on the error response.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.headers = headers or {}

    def to_response(self, request: Request) -> Response:
        """Render this error as the shared API envelope, replaying any headers."""
        response = api_error_response(
            code=self.code, message=self.message, status_code=self.status_code, request=request
        )
        for header, value in self.headers.items():
            response[header] = value
        return response


class HasActiveCTFActor(permissions.BasePermission):
    """Require one current human, service, personal, or temporary credential."""

    message = "API token is not associated with an active user."

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            authenticated_credential(request)
        except ValueError:
            return False
        return True


class HasCTFEventAdminAccess(permissions.BasePermission):
    """Admit non-temporary credentials; each operation makes the exact policy decision."""

    message = "Forbidden"

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            credential = authenticated_credential(request)
        except ValueError:
            return False
        return credential.kind != "temporary"


class HasCTFParticipant(permissions.BasePermission):
    """Require the resolved actor to be a viewing-eligible CTF participant.

    Uses the view predicate (CTF-609): disqualified participants keep read
    access to the me-surface; every mutation service re-checks compete
    eligibility, so widening this gate cannot let them act or rank.
    """

    message = "Forbidden"

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = ctf_actor_user(request)
        return bool(user and is_viewing_participant(user))


class HasCTFRole(permissions.BasePermission):
    """Require the resolved actor to have any CTF role."""

    message = "Forbidden"

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = ctf_actor_user(request)
        if user is None:
            return False
        role = get_user_role(user)
        return role.is_ctf_organizer or role.is_ctf_participant


class HasCTFEndpointScope(permissions.BasePermission):
    """Admit API-token requests only when they carry one declared CTF scope."""

    message = "API token is missing the required scope."

    def has_permission(self, request: Request, view: APIView) -> bool:
        auth = getattr(request, "auth", None)
        if not isinstance(auth, ApiToken) and not (
            isinstance(auth, CredentialContext) and auth.kind in {"personal", "service"}
        ):
            return True
        required = _required_scopes_for_method(request.method, view)
        return any(has_scope(auth.scopes, scope) for scope in required)


CTF_AUTH_PERMISSIONS: list[PermissionClass] = [
    IsAuthenticatedSessionOrApiToken,
    HasActiveCTFActor,
    HasCTFEndpointScope,
]

CTF_ORGANIZER_PERMISSIONS: list[PermissionClass] = [*CTF_AUTH_PERMISSIONS, HasCTFEventAdminAccess]
CTF_PARTICIPANT_PERMISSIONS: list[PermissionClass] = [*CTF_AUTH_PERMISSIONS, HasCTFParticipant]
CTF_ROLE_PERMISSIONS: list[PermissionClass] = [*CTF_AUTH_PERMISSIONS, HasCTFRole]


def _required_scopes_for_method(method: str, view: APIView) -> Iterable[str]:
    """Return the declared CTF token scopes for the incoming HTTP method."""
    scopes = getattr(view, "required_read_scopes", ()) if method in permissions.SAFE_METHODS else ()
    if method not in permissions.SAFE_METHODS:
        scopes = getattr(view, "required_write_scopes", ())
    return tuple(scopes)


def _canonical_error_response(request: Request, response: HttpResponse) -> Response | None:
    """Convert canonical CTF legacy flat-error JSON to the shared API envelope."""
    message = _legacy_error_message(request, response)
    if message is None:
        return None

    converted = api_error_response(
        code=_error_code_for_status(response.status_code),
        message=message,
        status_code=response.status_code,
        request=request,
    )
    for header in ("Retry-After", "Location"):
        if response.has_header(header):
            converted[header] = response[header]
    return converted


def _legacy_error_message(request: Request, response: HttpResponse) -> str | None:
    """Extract a flat legacy error message when a response is eligible."""
    message = None
    if request.path.startswith("/api/v1/ctf/") and response.status_code >= 400 and isinstance(response, JsonResponse):
        try:
            payload = json.loads(response.content.decode(response.charset or "utf-8"))
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            message = cast(str, payload["error"])
    return message


def _error_code_for_status(status_code: int) -> str:
    """Map legacy CTF HTTP statuses onto the shared API error-code vocabulary."""
    return {
        400: "bad_request",
        403: "permission_denied",
        404: "not_found",
        429: "throttled",
    }.get(status_code, "ctf_error")


def ctf_error_response(request: Request, exc: Exception) -> Response:
    """One authored, body-free CTF-to-platform error mapping."""
    from ctf.exceptions import (
        CTFCommunicationError,
        CTFNotFoundError,
        CTFPermissionError,
        CTFStateError,
        CTFValidationError,
    )

    code, message, status_code = "dependency_unavailable", "Service unavailable", 503
    if isinstance(exc, CTFCommunicationError):
        domain_code = exc.code
        if domain_code in {
            "CTF_COMMUNICATION_ACTOR_DENIED",
            "CTF_COMMUNICATION_ACTOR_REQUIRED",
            "CTF_COMMUNICATION_TOKEN_DENIED",
            "CTF_COMMUNICATION_WORKSPACE_DENIED",
            "CTF_COMMUNICATION_EVENT_DENIED",
            "CTF_COMMUNICATION_TARGET_DENIED",
            "CTF_COMMUNICATION_NOT_FOUND",
            "CTF_COMMUNICATION_REVISION_MISMATCH",
            "CTF_COMMUNICATION_AUDIENCE_OUT_OF_SCOPE",
            "CTF_COMMUNICATION_EARLY_RELEASE_DENIED",
            "CTF_COMMUNICATION_PARTICIPANT_DENIED",
        }:
            code, message, status_code = "not_found", "Resource not found", 404
        elif domain_code in {"CTF_COMMUNICATION_RATE_LIMITED", "CTF_COMMUNICATION_BACKLOG_FULL"}:
            code, message, status_code = "throttled", "Request was throttled", 429
        elif domain_code in {
            "CTF_COMMUNICATION_CONTENT_INVALID",
            "CTF_COMMUNICATION_TRIGGER_INVALID",
            "CTF_COMMUNICATION_NO_TARGETS",
            "CTF_COMMUNICATION_SOURCE_UNKNOWN",
            "CTF_COMMUNICATION_AUDIENCE_TOO_LARGE",
        }:
            code, message, status_code = "invalid", "Invalid request", 400
        elif domain_code in {
            "CTF_COMMUNICATION_NOT_DRAFT",
            "CTF_COMMUNICATION_NOT_DUE",
            "CTF_COMMUNICATION_DUE_MISMATCH",
            "CTF_COMMUNICATION_LIFECYCLE_NOT_SCHEDULABLE",
            "CTF_COMMUNICATION_MILESTONE_NOT_REACHED",
            "CTF_COMMUNICATION_IDEMPOTENCY_MISMATCH",
            "CTF_COMMUNICATION_REPLAY_CONFLICT",
            "CTF_COMMUNICATION_DECLARATION_CONFLICT",
            "CTF_COMMUNICATION_EVENT_CANCELLED",
            "CTF_COMMUNICATION_NO_CONTENT",
            "CTF_COMMUNICATION_CANCELLED",
            "CTF_COMMUNICATION_ACK_POLICY",
        }:
            code, message, status_code = "conflict", "Request conflicts with current state", 409
    else:
        for error_type, mapped in (
            (CTFNotFoundError, ("not_found", "Resource not found", 404)),
            (CTFPermissionError, ("forbidden", "Permission denied", 403)),
            (CTFValidationError, ("invalid", "Invalid request", 400)),
            (CTFStateError, ("conflict", "Request conflicts with current state", 409)),
        ):
            if isinstance(exc, error_type):
                code, message, status_code = mapped
                break
    response = api_error_response(code=code, message=message, status_code=status_code, request=request)
    if status_code == 429:
        response["Retry-After"] = "60"
    return response
