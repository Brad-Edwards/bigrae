"""Event staff and ownership authority-topology views (CTF-607, #1922).

Staff management (moderators, judges, co-organizers) and canonical-ownership
transfer stay with the owning organizer — capability delegation never includes
delegating further, which is what keeps a co-organizer from escalating itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import CTF_ORGANIZER_PERMISSIONS, _CtfApiError
from ctf.api.organizer._audit import (
    admin_external_audit,
)
from ctf.api.organizer._base import (
    _EVENT_READ,
    _EVENT_WRITE,
    _credential,
    _resolve_owned_event,
)
from ctf.api.serializers import (
    EventMutationResultSerializer,
    EventOwnershipTransferRequestSerializer,
    EventStaffAssignRequestSerializer,
    EventStaffListResponseSerializer,
    EventStaffMemberSerializer,
    ParticipantDeleteResultSerializer,
)
from ctf.exceptions import CTFError, CTFNotFoundError, CTFPermissionError, CTFStateError, CTFValidationError
from shared.audit import AuditAction

_EVENT_MANAGE_STAFF = "event.manage_staff"

if TYPE_CHECKING:
    from uuid import UUID

    from ctf.models import CTFEventStaff

logger = logging.getLogger(__name__)


def _authority_error(error: CTFError) -> _CtfApiError:
    """Keep known domain failures identical across authority commands and reads."""
    if isinstance(error, CTFNotFoundError):
        result = _CtfApiError(status_code=404, code="not_found", message=str(error))
    elif isinstance(error, CTFPermissionError):
        result = _CtfApiError(status_code=403, code="permission_denied", message="Forbidden")
    elif isinstance(error, CTFValidationError):
        result = _CtfApiError(status_code=400, code="invalid", message=str(error))
    elif isinstance(error, CTFStateError) or error.code in {
        "CTF_AUTHORITY_UNAVAILABLE",
        "CTF_EVENT_AUTHORITY_UNAVAILABLE",
    }:
        result = _CtfApiError(
            status_code=503, code="event_authority_unavailable", message="Event authority is unavailable."
        )
    else:
        raise error
    return result


def _staff_payload(staff: CTFEventStaff) -> dict[str, object]:
    """Render one staff assignment row."""
    return {
        "principal_uuid": staff.principal_uuid,
        "role": staff.role,
        "created_at": staff.created_at.isoformat() if staff.created_at else None,
    }


class EventStaffView(APIView):
    """List (GET) or assign (POST) delegated staff on an owned event."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ
    required_write_scopes = _EVENT_WRITE

    @extend_schema(responses=EventStaffListResponseSerializer)
    def get(self, request: Request, event_id: UUID) -> Response:
        """Return the event's staff assignments (owner-only)."""
        from ctf.services.event import list_event_staff, pending_event_authority

        try:
            _resolve_owned_event(request, event_id, action=_EVENT_MANAGE_STAFF)
            pending = pending_event_authority(event_id, _credential(request))
        except CTFError as exc:
            return _authority_error(exc).to_response(request)
        except _CtfApiError as exc:
            return exc.to_response(request)
        return Response(
            {"staff": [_staff_payload(s) for s in list_event_staff(event_id)], "pending_transition": pending}
        )

    @extend_schema(request=EventStaffAssignRequestSerializer, responses={201: EventStaffMemberSerializer})
    def post(self, request: Request, event_id: UUID) -> Response:
        """Assign (or re-role) a staff member by canonical principal UUID."""
        from ctf.services.event import assign_event_staff

        try:
            serializer = EventStaffAssignRequestSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            try:
                _resolve_owned_event(request, event_id, action=_EVENT_MANAGE_STAFF)
                with admin_external_audit(request, "staff.assign", action=AuditAction.CREATE):
                    staff = assign_event_staff(
                        event_id,
                        _credential(request),
                        serializer.validated_data["principal_uuid"],
                        serializer.validated_data["role"],
                    )
            except CTFError as exc:
                raise _authority_error(exc) from None
            return Response(_staff_payload(staff), status=status.HTTP_201_CREATED)
        except _CtfApiError as exc:
            return exc.to_response(request)


class EventStaffMemberView(APIView):
    """Revoke (DELETE) one staff assignment on an owned event."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(responses=ParticipantDeleteResultSerializer)
    def delete(self, request: Request, event_id: UUID, principal_uuid: UUID) -> Response:
        """Remove the assignment; the user keeps their platform account."""
        from ctf.services.event import revoke_event_staff

        try:
            try:
                _resolve_owned_event(request, event_id, action=_EVENT_MANAGE_STAFF)
                with admin_external_audit(request, "staff.revoke", action=AuditAction.DELETE):
                    revoke_event_staff(event_id, _credential(request), principal_uuid)
            except CTFError as exc:
                raise _authority_error(exc) from None
            return Response(status=204)
        except _CtfApiError as exc:
            return exc.to_response(request)


class EventOwnershipTransferView(APIView):
    """Transfer canonical ownership to a co-organizer (POST, owner-only, #1922)."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(request=EventOwnershipTransferRequestSerializer, responses=EventMutationResultSerializer)
    def post(self, request: Request, event_id: UUID) -> Response:
        """Promote a current co-organizer to owner; the previous owner stays a co-organizer."""
        from ctf.services.event import transfer_event_ownership

        try:
            serializer = EventOwnershipTransferRequestSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            try:
                event = transfer_event_ownership(
                    event_id,
                    _credential(request),
                    serializer.validated_data["principal_uuid"],
                )
            except CTFError as exc:
                raise _authority_error(exc) from None
            return Response({"id": str(event.id), "name": event.name, "status": event.status})
        except _CtfApiError as exc:
            return exc.to_response(request)
