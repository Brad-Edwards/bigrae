"""Range lifecycle views for the canonical CTF API.

Participant range status/access plus the organizer range operations
(bulk provisioning, spare pool, per-participant lifecycle actions, recovery).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import CTF_PARTICIPANT_PERMISSIONS, _CtfApiError
from ctf.api.organizer._base import (
    _actor,
    _raise_not_found,
    _resolve_active_participant,
)
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api.schema import ApiErrorSerializer
from shared.api_tokens import scopes
from shared.credentials import CredentialContext
from shared.remote_access import OPENVPN_PROFILE_MEDIA_TYPE

if TYPE_CHECKING:
    from uuid import UUID

    from rest_framework.permissions import BasePermission

    from ctf.models import CTFParticipant
    from shared.remote_access import OpenVpnProfile


_VPN_PROFILE_READ = (scopes.CTF_VPN_PROFILE_READ,)
_VPN_PROFILE_FILENAME = "shifter-ctf-range.ovpn"
_VPN_PROFILE_NOT_FOUND = "VPN profile is not available."
_VPN_PROFILE_NOT_READY = "VPN profile is not ready."
_VPN_PROFILE_UNAVAILABLE = "VPN profile is temporarily unavailable."


class ParticipantVpnProfileView(APIView):
    """Deliver the current participant's generation-bound OpenVPN profile."""

    permission_classes = CTF_PARTICIPANT_PERMISSIONS
    required_write_scopes = _VPN_PROFILE_READ

    def get_permissions(self) -> list[BasePermission]:
        if isinstance(getattr(self.request, "auth", None), CredentialContext) or settings.OPENFGA_ENABLED:
            return [IsAuthenticatedSessionOrApiToken()]
        return super().get_permissions()

    @extend_schema(
        request=None,
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
        """Return a no-store credential after role, ownership, state and rate gates."""
        from ctf.services.audit import audit_vpn_profile_download

        try:
            source = isinstance(getattr(request, "auth", None), CredentialContext) or settings.OPENFGA_ENABLED
            credential = None
            if request.body or (set(request.query_params) - {"event_id"} if source else request.query_params):
                raise _CtfApiError(
                    code="invalid",
                    message="VPN profile requests must not include a body or query parameters.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            credential, participant, range_instance_id = _vpn_profile_actor(request, source=source)
            _admit_vpn_delivery_rate(credential.principal.uuid if credential else _actor(request).pk)
            profile = _participant_profile(participant, credential)
            audit_vpn_profile_download(
                actor_id=None if credential else _actor(request).pk,
                **({"principal": credential.principal} if credential else {}),
                participant_id=participant.pk,
                range_instance_id=range_instance_id,
                generation=profile.generation,
                profile_version=profile.profile_version,
            )
            response = HttpResponse(profile.content, content_type=OPENVPN_PROFILE_MEDIA_TYPE)
            response["Content-Disposition"] = f'attachment; filename="{_VPN_PROFILE_FILENAME}"'
            response["Cache-Control"] = "private, no-store"
            response["Content-Length"] = str(len(profile.content))
            patch_vary_headers(response, ("Cookie", "Authorization"))
            return response
        except _CtfApiError as exc:
            return exc.to_response(request)


def _vpn_profile_actor(request: Request, *, source: bool) -> tuple[CredentialContext | None, CTFParticipant, int]:
    """Resolve the exact active participation through its existing principal or incumbent service boundary."""
    from ctf.exceptions import CTFError
    from ctf.services import range as range_service

    credential = None
    if source:
        try:
            credential = (
                request.auth if isinstance(request.auth, CredentialContext) else authenticated_credential(request)
            )
            event_id = UUID(request.query_params["event_id"]) if "event_id" in request.query_params else None
            participant: CTFParticipant | None = range_service.vpn_participant_for_credential(
                credential, event_uuid=event_id
            )
        except (ValueError, CTFError):
            _raise_not_found(_VPN_PROFILE_NOT_FOUND)
    else:
        participant = _resolve_active_participant(request)
    if participant is None or participant.range_instance_id is None:
        _raise_not_found(_VPN_PROFILE_NOT_FOUND)
    return credential, participant, participant.range_instance_id


def _admit_vpn_delivery_rate(actor_id: UUID | int) -> None:
    """Apply the existing credential-delivery budget before reading private VPN material."""
    from ctf.views._access import _check_credential_delivery_rate_limit

    try:
        allowed = _check_credential_delivery_rate_limit(actor_id)
    except Exception as exc:
        raise _CtfApiError(
            code="vpn_profile_unavailable",
            message=_VPN_PROFILE_UNAVAILABLE,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    if not allowed:
        raise _CtfApiError(
            code="throttled",
            message="Too many VPN profile requests. Try again later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "3600"},
        )


def _participant_profile(participant: CTFParticipant, credential: CredentialContext | None) -> OpenVpnProfile:
    """Map existing range-profile states onto the canonical participant HTTP error contract."""
    from ctf.exceptions import CTFNotFoundError, CTFRangeError, CTFStateError, CTFVpnProfilePending
    from ctf.services import range as range_service

    try:
        profile = (
            range_service.get_vpn_profile(participant.pk, credential=credential)
            if credential
            else range_service.get_vpn_profile(participant.pk)
        )
    except CTFNotFoundError:
        _raise_not_found(_VPN_PROFILE_NOT_FOUND)
    except CTFVpnProfilePending as exc:
        raise _CtfApiError(
            code="vpn_preparing",
            message="Preparing your VPN profile.",
            status_code=202,
            headers={"Retry-After": "5"},
        ) from exc
    except CTFStateError as exc:
        raise _CtfApiError(
            code="vpn_not_ready",
            message=_VPN_PROFILE_NOT_READY,
            status_code=status.HTTP_409_CONFLICT,
        ) from exc
    except CTFRangeError as exc:
        raise _CtfApiError(
            code="vpn_profile_unavailable",
            message=_VPN_PROFILE_UNAVAILABLE,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return profile
