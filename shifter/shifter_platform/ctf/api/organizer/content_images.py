"""Organizer image map and the first-party image view (#2090, ADR-036).

The portal enforces ``img-src 'self' data:``. External images in organizer
content are copied into platform storage (``ctf.services.content_images``). The
organizer map shows every image of an event with its copy state, so an organizer
can see why an image shows as a link. The image view sends a ready copy from the
portal origin to event staff and to participants who can read the content that
uses it; every other request gets one opaque 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import CTF_AUTH_PERMISSIONS, CTF_ORGANIZER_PERMISSIONS, _CtfApiError
from ctf.api.organizer._base import (
    _EVENT_OR_PLAY_READ,
    _EVENT_READ,
    _credential,
    _raise_not_found,
    _resolve_owned_event,
)
from ctf.exceptions import CTFError
from shared.api.errors import api_error_response
from shared.api.schema import ApiErrorSerializer

if TYPE_CHECKING:
    from uuid import UUID

_IMAGE_NOT_FOUND = "Image not found"
# Bytes are immutable; access is revocable. Every fetch must reach admission.
_IMAGE_CACHE_CONTROL = "private, no-store"


class OrganizerContentImageSerializer(serializers.Serializer):
    """One external image of an event and its copy state."""

    source_url = serializers.URLField()
    status = serializers.ChoiceField(choices=["pending", "ready", "failed"])
    failure_code = serializers.CharField(allow_blank=True)
    url = serializers.CharField(allow_null=True)


class OrganizerContentImagesResponseSerializer(serializers.Serializer):
    """Envelope for the organizer image map."""

    images = OrganizerContentImageSerializer(many=True)


class EventContentImagesView(APIView):
    """List an event's external images and their first-party copies (GET)."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ

    @extend_schema(responses=OrganizerContentImagesResponseSerializer)
    def get(self, request: Request, event_id: UUID) -> Response:
        """Return every image row of the event, newest source first."""
        from ctf.models import ContentImageStatus, CTFContentImage
        from ctf.services.content_images import CONTENT_IMAGE_STAFF_CAPABILITY, image_url

        try:
            _resolve_owned_event(request, event_id, capability=CONTENT_IMAGE_STAFF_CAPABILITY)
        except _CtfApiError as exc:
            return exc.to_response(request)
        rows = CTFContentImage.objects.filter(event_id=event_id).order_by("-created_at")
        return Response(
            {
                "images": [
                    {
                        "source_url": row.source_url,
                        "status": row.status,
                        "failure_code": row.failure_code,
                        "url": image_url(row.id) if row.status == ContentImageStatus.READY else None,
                    }
                    for row in rows
                ]
            }
        )


class ContentImageView(APIView):
    """Send one ready first-party image copy (GET)."""

    permission_classes = CTF_AUTH_PERMISSIONS
    required_read_scopes = _EVENT_OR_PLAY_READ

    @extend_schema(responses={(200, "image/*"): OpenApiTypes.BINARY, 404: ApiErrorSerializer})
    def get(self, request: Request, image_id: UUID) -> HttpResponse | Response:
        """Return the verified bytes, or 404 for any image the actor cannot read."""
        from ctf.models import ContentImageStatus, CTFContentImage
        from ctf.services.content_images import can_read_content_image, read_content_image

        try:
            image = (
                CTFContentImage.objects.select_related("event")
                .filter(pk=image_id, status=ContentImageStatus.READY)
                .first()
            )
            if image is None or not can_read_content_image(_credential(request), image):
                _raise_not_found(_IMAGE_NOT_FOUND)
            data = read_content_image(image)
            if data is None:
                _raise_not_found(_IMAGE_NOT_FOUND)
        except _CtfApiError as exc:
            return exc.to_response(request)
        except CTFError:
            return api_error_response(
                code="dependency_unavailable", message="Service unavailable", status_code=503, request=request
            )
        response = HttpResponse(data, content_type=image.content_type)
        response["Content-Disposition"] = "inline"
        response["Content-Length"] = str(len(data))
        response["Cache-Control"] = _IMAGE_CACHE_CONTROL
        response["X-Content-Type-Options"] = "nosniff"
        patch_vary_headers(response, ("Cookie", "Authorization"))
        return response
