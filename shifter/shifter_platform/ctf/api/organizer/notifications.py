"""Organizer notification views for the canonical CTF API.

Announcements, notification dispatch, and the send-invitations action. The
per-event email-template override API is retired (#580).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import CTF_ORGANIZER_PERMISSIONS, _CtfApiError
from ctf.api.organizer._base import (
    _EVENT_READ,
    _EVENT_WRITE,
    _pagination_window,
    _resolve_owned_event,
)
from ctf.api.serializers import NotificationListResponseSerializer

logger = logging.getLogger(__name__)


class SendLoginInfoView(APIView):
    """Send invitation emails to all uninvited participants of an owned event."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(exclude=True)
    def post(self, request: Request, **kwargs: Any) -> Response:
        from ctf.api.retired_notifications import retired_notification_response

        return retired_notification_response(request)


class NotificationListView(APIView):
    """List an event's notifications (GET) or send an announcement (POST)."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ
    required_write_scopes = _EVENT_WRITE

    @extend_schema(responses=NotificationListResponseSerializer)
    def get(self, request: Request, event_id: UUID) -> Response:
        """Return the notifications for an owned event, newest first."""
        from ctf.models import CTFNotification

        try:
            event = _resolve_owned_event(request, event_id, capability="notifications")
        except _CtfApiError as exc:
            return exc.to_response(request)
        queryset = CTFNotification.objects.filter(event=event).order_by("-created_at")
        total = queryset.count()
        offset, limit = _pagination_window(request)
        notifications = queryset[offset : offset + limit] if limit is not None else queryset
        data = [
            {
                "id": str(n.id),
                "notification_type": n.notification_type,
                "subject": n.subject,
                "status": n.status,
                "sent_count": n.sent_count,
                "created_at": n.created_at.isoformat(),
                "sent_at": n.sent_at.isoformat() if n.sent_at else None,
                "scheduled_at": n.scheduled_at.isoformat() if n.scheduled_at else None,
            }
            for n in notifications
        ]
        return Response({"notifications": data, "total": total})

    @extend_schema(exclude=True)
    def post(self, request: Request, **kwargs: Any) -> Response:
        from ctf.api.retired_notifications import retired_notification_response

        return retired_notification_response(request)


class NotificationCancelScheduleView(APIView):
    """Cancel a scheduled notification before delivery (POST, CTF-804)."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(exclude=True)
    def post(self, request: Request, **kwargs: Any) -> Response:
        from ctf.api.retired_notifications import retired_notification_response

        return retired_notification_response(request)


class NotificationSendView(APIView):
    """Dispatch a notification to its recipients (organizer)."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(exclude=True)
    def post(self, request: Request, **kwargs: Any) -> Response:
        from ctf.api.retired_notifications import retired_notification_response

        return retired_notification_response(request)
