"""Shared helpers for Mission Control DRF views."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import HttpRequest
from rest_framework import permissions, serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from mission_control.api.permissions import (
    HasMissionControlActionCeiling,
    HasMissionControlActor,
    mission_control_actor_user,
)
from shared.api.errors import api_error_response
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.credentials import CredentialContext, resolve_current_credential
from shared.exceptions import CMSError

PermissionClass = type[permissions.BasePermission]
ValidationResult = tuple[dict[str, Any], None] | tuple[None, Response]


class MissionControlAPIView(APIView):
    """Base class for authenticated Mission Control DRF endpoints."""

    permission_classes = [IsAuthenticatedSessionOrApiToken, HasMissionControlActor]
    required_actions: ClassVar[tuple[str, ...]] = ("range.manage",)

    def get_permissions(self) -> list[permissions.BasePermission]:
        """The owning service still checks the exact target and live policy."""
        return [*super().get_permissions(), HasMissionControlActionCeiling()]

    def handle_exception(self, exc: Exception) -> Response:
        """Preserve bounded domain denials before delegating other DRF failures."""
        if isinstance(exc, (CMSError, PermissionError)):
            return self.error_response(code="forbidden", message="Resource unavailable", status_code=403)
        if isinstance(exc, ValueError):
            return self.bad_request("Resource scope unavailable")
        return super().handle_exception(exc)

    def actor_credential(self) -> CredentialContext:
        """Retain the exact proof for every owning-service operation."""
        try:
            return resolve_current_credential(authenticated_credential(self.request))
        except ValueError:
            raise DjangoPermissionDenied("Authenticated credential unavailable") from None

    def actor_user(self) -> User:
        """Return the user whose Mission Control resources this request acts on."""
        user = mission_control_actor_user(self.request)
        if user is None:
            raise DjangoPermissionDenied("Authenticated user unavailable")
        return user

    def invalid(self, serializer: serializers.Serializer) -> Response:
        """Return validation errors in the canonical API error format."""
        return api_error_response(
            code="invalid",
            message="Invalid request",
            status_code=status.HTTP_400_BAD_REQUEST,
            details=serializer.errors,
            detail_serializer=serializer,
            request=self.request,
        )

    def bad_request(self, message: str) -> Response:
        """Return a 400 response in the canonical API error format."""
        return api_error_response(
            code="bad_request",
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            request=self.request,
        )

    def not_found(self, message: str) -> Response:
        """Return a 404 response in the canonical API error format."""
        return api_error_response(
            code="not_found",
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
            request=self.request,
        )

    def error_response(self, *, code: str, message: str, status_code: int) -> Response:
        """Return a structured error in the canonical API error format."""
        return api_error_response(code=code, message=message, status_code=status_code, request=self.request)


class MissionControlReadAPIView(MissionControlAPIView):
    """Read-only Mission Control endpoint."""

    permission_classes = [IsAuthenticatedSessionOrApiToken, HasMissionControlActor]
    required_actions: ClassVar[tuple[str, ...]] = ("range.read",)


def _raw_request(drf_request: Request) -> HttpRequest:
    """Return the underlying Django request while preserving DRF auth state."""
    raw = getattr(drf_request, "_request", drf_request)
    raw.auth = getattr(drf_request, "auth", None)
    return cast(HttpRequest, raw)


def _validated(
    view: MissionControlAPIView,
    serializer_class: type[serializers.Serializer],
    data: object,
) -> ValidationResult:
    """Validate request data with a DRF serializer and return data or response."""
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        return None, view.invalid(serializer)
    return cast(dict[str, Any], serializer.validated_data), None


def _catalog_error(view: MissionControlAPIView, exc: CMSError) -> Response:
    """Translate content denial and provider unavailability without provider text."""
    unavailable = exc.details.get("code") == "authority-unavailable"
    return view.error_response(
        code="catalog_unavailable" if unavailable else "content_not_available",
        message="Content catalog is not available.",
        status_code=503 if unavailable else 403,
    )
