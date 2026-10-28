"""Mission Control OpenVPN profile delivery view, split from ranges (python:S104)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import get_mission_control_openvpn_profile
from mission_control.api._base import MissionControlAPIView
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api.schema import ApiErrorSerializer
from shared.credentials import CredentialContext
from shared.remote_access import OPENVPN_PROFILE_MEDIA_TYPE, OpenVpnProfile

if TYPE_CHECKING:
    from uuid import UUID

    from shared.credentials import CredentialContext


_VPN_PROFILE_FILENAME = "shifter-range.ovpn"


def _credential_delivery_error(view: MissionControlAPIView, actor: User | CredentialContext) -> Response | None:
    """Return a fail-closed delivery response, or None when delivery may proceed."""
    from shared.credential_delivery import credential_delivery_allowed

    try:
        allowed = credential_delivery_allowed(
            actor.principal.uuid if isinstance(actor, CredentialContext) else actor.pk
        )
    except Exception:
        response = view.error_response(
            code="vpn_profile_unavailable",
            message="VPN profile is unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    else:
        response = None
        if not allowed:
            response = view.error_response(
                code="throttled",
                message="Too many VPN profile requests. Try again later.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response["Retry-After"] = "3600"
    return response


def _mission_control_profile_result(
    view: MissionControlAPIView,
    actor: User | CredentialContext,
    request_id: UUID | None = None,
) -> tuple[OpenVpnProfile, int] | Response:
    """Resolve a profile or translate the CMS failure into its API response."""
    from cms.services import (
        OpenVpnProfileConflict,
        OpenVpnProfileNotFound,
        OpenVpnProfilePending,
        OpenVpnProfileUnavailable,
    )

    result: tuple[OpenVpnProfile, int] | Response
    try:
        result = (
            get_mission_control_openvpn_profile(actor, request_id=request_id)
            if request_id is not None
            else get_mission_control_openvpn_profile(actor)
        )
    except OpenVpnProfilePending:
        result = view.error_response(code="vpn_preparing", message="Preparing your VPN profile.", status_code=202)
        result["Retry-After"] = "5"
    except OpenVpnProfileNotFound:
        result = view.not_found("VPN profile is not available.")
    except OpenVpnProfileConflict:
        result = view.error_response(
            code="vpn_not_ready",
            message="VPN profile is not ready.",
            status_code=status.HTTP_409_CONFLICT,
        )
    except OpenVpnProfileUnavailable:
        result = view.error_response(
            code="vpn_profile_unavailable",
            message="VPN profile is unavailable.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return result


def _vpn_profile_download_response(
    actor: User | CredentialContext, profile: OpenVpnProfile, range_instance_id: int
) -> HttpResponse:
    """Audit and build the non-cacheable OpenVPN credential response."""
    from shared.credential_delivery import audit_openvpn_profile_download

    audit_openvpn_profile_download(
        actor_id=None if isinstance(actor, CredentialContext) else actor.pk,
        principal=actor.principal if isinstance(actor, CredentialContext) else None,
        range_instance_id=range_instance_id,
        generation=profile.generation,
        profile_version=profile.profile_version,
        product="mission_control",
    )
    response = HttpResponse(profile.content, content_type=OPENVPN_PROFILE_MEDIA_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{_VPN_PROFILE_FILENAME}"'
    response["Cache-Control"] = "private, no-store"
    response["Content-Length"] = str(len(profile.content))
    patch_vary_headers(response, ("Cookie", "Authorization"))
    return response


class MissionControlVpnProfileView(MissionControlAPIView):
    """Deliver the current Mission Control range's generation-bound OpenVPN profile."""

    required_actions = ("range.access",)

    permission_classes = [IsAuthenticatedSessionOrApiToken]

    @extend_schema(
        request=None,
        parameters=[OpenApiParameter("request_id", type=OpenApiTypes.UUID, required=False)],
        responses={
            (200, OPENVPN_PROFILE_MEDIA_TYPE): OpenApiTypes.BINARY,
            202: ApiErrorSerializer,
            400: ApiErrorSerializer,
            404: ApiErrorSerializer,
            409: ApiErrorSerializer,
            429: ApiErrorSerializer,
            503: ApiErrorSerializer,
        },
    )
    def post(self, request: Request) -> HttpResponse | Response:
        return self._principal_profile(request)

    def _principal_profile(self, request: Request) -> HttpResponse | Response:
        """Validate the selected principal range before rate admission and private profile delivery."""
        try:
            credential = (
                request.auth if isinstance(request.auth, CredentialContext) else authenticated_credential(request)
            )
            if request.body or set(request.query_params) - {"request_id"}:
                raise ValueError("Invalid profile request")
            request_id = UUID(request.query_params["request_id"]) if "request_id" in request.query_params else None
            delivery_error = _credential_delivery_error(self, credential)
            if delivery_error is not None:
                response = delivery_error
            else:
                result = _mission_control_profile_result(self, credential, request_id=request_id)
                if isinstance(result, Response):
                    response = result
                else:
                    profile, range_instance_id = result
                    response = _vpn_profile_download_response(credential, profile, range_instance_id)
            return response
        except ValueError:
            return self.error_response(code="invalid", message="VPN profile request is invalid.", status_code=400)
