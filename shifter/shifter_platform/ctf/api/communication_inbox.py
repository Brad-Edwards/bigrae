"""Session-only communication inboxes; reads never mutate receipts.

The participant inbox serves participant rows; the organizer alert inbox serves
organizer rows to the event owner and live co-organizers (ADR-065-R4). Every
response is private and uncacheable, and invalid input never returns its own
text (#2053).
"""

from __future__ import annotations

from uuid import UUID

from django.conf import settings
from django.utils.cache import add_never_cache_headers
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import ctf_error_response
from ctf.api.communication_schema import SessionCommunicationSchema
from ctf.api.organizer.communication import validate_query
from ctf.api.serializers.communication import (
    CommunicationEmptySerializer,
    CommunicationInboxItemSerializer,
    CommunicationJSONParser,
)
from ctf.enums_communication import InboxFilter, ReceiptAction
from ctf.exceptions import CTFCommunicationError
from ctf.services.communication.inbox import (
    inbox_counts,
    inbox_for_participant,
    interact_with_organizer_receipt,
    interact_with_receipt,
    organizer_inbox,
    select_inbox_view,
)
from shared.api.closed_serializer import ClosedSerializer
from shared.api.errors import api_error_response
from shared.api.permissions import IsAuthenticatedSession
from shared.api.principals import authenticated_credential


class InboxQuerySerializer(ClosedSerializer):
    """Bound the participant inbox page size and offset."""

    limit = serializers.IntegerField(min_value=1, max_value=100, default=25)
    offset = serializers.IntegerField(min_value=0, max_value=100000, default=0)
    view = serializers.ChoiceField(choices=[f.value for f in InboxFilter], default=InboxFilter.INBOX.value)


class InboxPageSerializer(serializers.Serializer):
    """Project one inbox page, its continuation offset, and inbox-wide counts."""

    results = CommunicationInboxItemSerializer(many=True)
    next_offset = serializers.IntegerField(allow_null=True)
    unread_count = serializers.IntegerField(help_text="Unread messages in the default view")
    action_required_count = serializers.IntegerField(help_text="Messages whose pinned policy needs an action")
    allowed_link_hosts = serializers.ListField(
        child=serializers.CharField(), help_text="Deployment HTTPS link hosts the renderer may link to"
    )


class InboxView(APIView):
    """Apply session-only policy and canonical inbox error responses."""

    schema = SessionCommunicationSchema()
    permission_classes = [IsAuthenticatedSession]
    parser_classes = [CommunicationJSONParser]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, CTFCommunicationError):
            return ctf_error_response(self.request, exc)
        if isinstance(exc, serializers.ValidationError):
            return api_error_response(code="invalid", message="Invalid request", status_code=400, request=self.request)
        return super().handle_exception(exc)

    def finalize_response(self, request: Request, response: Response, *args: object, **kwargs: object) -> Response:
        """Mark every inbox response, errors included, private and uncacheable."""
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


class CommunicationInboxView(InboxView):
    """List retained messages for the current participant and event."""

    @extend_schema(
        operation_id="ctf_communication_inbox_list",
        parameters=[InboxQuerySerializer],
        responses=InboxPageSerializer,
    )
    def get(self, request: Request, event_id: UUID) -> Response:
        query = validate_query(request, InboxQuerySerializer)
        start, limit = query["offset"], query["limit"]
        retained = inbox_for_participant(request.user, event_id)
        rows = list(select_inbox_view(retained, InboxFilter(query["view"]))[start : start + limit + 1])
        return Response(
            {
                "results": CommunicationInboxItemSerializer(rows[:limit], many=True).data,
                "next_offset": start + limit if len(rows) > limit else None,
                **inbox_counts(retained),
                "allowed_link_hosts": sorted(settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS),
            }
        )


class CommunicationInboxDetailView(InboxView):
    """Fetch one message within the authorized participant inbox."""

    @extend_schema(responses=CommunicationInboxItemSerializer)
    def get(self, request: Request, event_id: UUID, snapshot_id: UUID) -> Response:
        validate_query(request, CommunicationEmptySerializer)
        snapshot = inbox_for_participant(request.user, event_id).filter(pk=snapshot_id).first()
        if snapshot is None:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
        return Response(CommunicationInboxItemSerializer(snapshot).data)


class CommunicationReadView(InboxView):
    """Record an explicit, idempotent read interaction."""

    action = ReceiptAction.READ

    @extend_schema(request=CommunicationEmptySerializer, responses=CommunicationInboxItemSerializer)
    def post(self, request: Request, event_id: UUID, snapshot_id: UUID) -> Response:
        validate_query(request, CommunicationEmptySerializer)
        serializer = CommunicationEmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        snapshot = interact_with_receipt(request.user, event_id, snapshot_id, self.action)
        return Response(CommunicationInboxItemSerializer(snapshot).data)


class CommunicationAcknowledgeView(CommunicationReadView):
    """Record acknowledgement under the pinned message policy."""

    action = ReceiptAction.ACKNOWLEDGE


class CommunicationDismissView(CommunicationReadView):
    """Hide a message from the default view without reading or acknowledging it."""

    action = ReceiptAction.DISMISS


class OrganizerAlertInboxView(InboxView):
    """List organizer alerts for the current organizer and event."""

    @extend_schema(
        operation_id="ctf_organizer_alert_inbox_list",
        parameters=[InboxQuerySerializer],
        responses=InboxPageSerializer,
    )
    def get(self, request: Request, event_id: UUID) -> Response:
        """Return one page of the current organizer's alerts and inbox-wide counts."""
        query = validate_query(request, InboxQuerySerializer)
        start, limit = query["offset"], query["limit"]
        retained = organizer_inbox(authenticated_credential(request), event_id)
        rows = list(select_inbox_view(retained, InboxFilter(query["view"]))[start : start + limit + 1])
        return Response(
            {
                "results": CommunicationInboxItemSerializer(rows[:limit], many=True).data,
                "next_offset": start + limit if len(rows) > limit else None,
                **inbox_counts(retained),
                "allowed_link_hosts": sorted(settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS),
            }
        )


class OrganizerAlertDetailView(InboxView):
    """Fetch one alert within the authorized organizer inbox."""

    @extend_schema(operation_id="ctf_organizer_alert_inbox_detail", responses=CommunicationInboxItemSerializer)
    def get(self, request: Request, event_id: UUID, snapshot_id: UUID) -> Response:
        """Return one alert from the authorized organizer inbox."""
        validate_query(request, CommunicationEmptySerializer)
        snapshot = organizer_inbox(authenticated_credential(request), event_id).filter(pk=snapshot_id).first()
        if snapshot is None:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
        return Response(CommunicationInboxItemSerializer(snapshot).data)


class OrganizerAlertReadView(InboxView):
    """Record an explicit, idempotent organizer read."""

    action = ReceiptAction.READ

    @extend_schema(request=CommunicationEmptySerializer, responses=CommunicationInboxItemSerializer)
    def post(self, request: Request, event_id: UUID, snapshot_id: UUID) -> Response:
        """Record this view's receipt action once and return the alert."""
        validate_query(request, CommunicationEmptySerializer)
        serializer = CommunicationEmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        snapshot = interact_with_organizer_receipt(
            authenticated_credential(request), event_id, snapshot_id, self.action
        )
        return Response(CommunicationInboxItemSerializer(snapshot).data)


class OrganizerAlertDismissView(OrganizerAlertReadView):
    """Hide an organizer alert from the default view without reading it."""

    action = ReceiptAction.DISMISS
