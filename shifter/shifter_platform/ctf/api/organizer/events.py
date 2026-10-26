"""Organizer event views for the canonical CTF API (events + scenarios)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ctf.api._base import CTF_ORGANIZER_PERMISSIONS, _CtfApiError
from ctf.api.organizer._audit import (
    _audit_admin_mutation,
    admin_external_audit,
)
from ctf.api.organizer._base import (
    _EVENT_NOT_FOUND,
    _EVENT_READ,
    _EVENT_WRITE,
    _actor_may_manage,
    _credential,
    _event_authority,
    _raise_bad_request,
    _raise_forbidden,
    _raise_invalid_event,
    _raise_not_found,
    _resolve_owned_event,
)
from ctf.api.serializers import (
    CtfScenarioListResponseSerializer,
    EventDetailSerializer,
    EventListQuerySerializer,
    EventListResponseSerializer,
    EventMutationResultSerializer,
    EventSummarySerializer,
    EventWriteSerializer,
    ForceDeleteEventRequestSerializer,
    ForceDeleteEventResultSerializer,
)
from ctf.enums import EventCapability
from shared.api.strict_json import ModelSelectionJSONParser

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any
    from uuid import UUID

    from ctf.models import CTFEvent

# Canonical page-size bound for the authority-aware event list. The platform-admin
# path (all live events) is always bounded to this even when the caller omits
# pagination; an ordinary organizer keeps the historical full-list response
# (ADR-052-R3, ADR-040).
_EVENT_LIST_PAGE_SIZE = 200


def _authorized_event_page(events, credential, *, page: int | None, page_size: int | None) -> list[CTFEvent]:
    """Apply the requested page after policy filtering, in bounded database batches."""
    from ctf.exceptions import CTFPermissionError
    from ctf.services.unified_authorization import require_event_action

    size = page_size or _EVENT_LIST_PAGE_SIZE
    start = ((page or 1) - 1) * size if page is not None or page_size is not None else 0
    stop = start + size
    readable: list[CTFEvent] = []
    offset = 0
    while len(readable) < stop:
        batch = list(events[offset : offset + _EVENT_LIST_PAGE_SIZE])
        if not batch:
            break
        offset += len(batch)
        for event in batch:
            try:
                require_event_action(credential, event.pk, "event.read")
            except CTFPermissionError:
                continue
            readable.append(event)
            if len(readable) == stop:
                break
    return readable[start:stop]


def _event_projection_context(request: Request, credential, events: Sequence[CTFEvent]) -> dict[str, Any]:
    """Build advisory capabilities from the same live decisions as mutations."""
    from ctf.services.credential_scope import event_credential_scope
    from ctf.services.event.staff import capability_action_pairs
    from shared.authorization import AuthorizationRequest, TargetRef, configured_authorization_provider

    context: dict[str, Any] = {"request": request, "access_capabilities": {}}
    indexed: list[tuple[UUID, str]] = []
    checks: list[AuthorizationRequest] = []
    for event in events:
        scope = event_credential_scope(event.pk)
        for capability, action in capability_action_pairs():
            indexed.append((event.pk, capability))
            checks.append(
                AuthorizationRequest(
                    credential.principal,
                    action,
                    TargetRef("event", event.pk),
                    scope,
                    credential.ceiling,
                )
            )
    if checks:
        try:
            decisions = configured_authorization_provider().batch_check(tuple(checks))
        except Exception:
            decisions = ()
        if len(decisions) == len(indexed) and all(decision.kind != "evaluator_error" for decision in decisions):
            capabilities: dict[UUID, list[str]] = {}
            for (event_id, capability), decision in zip(indexed, decisions, strict=True):
                if decision.allowed:
                    capabilities.setdefault(event_id, []).append(capability)
            context["access_capabilities"] = capabilities
    return context


class EventListView(APIView):
    """List the organizer's events (GET) or create one (POST)."""

    parser_classes = [ModelSelectionJSONParser, FormParser, MultiPartParser]

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ
    required_write_scopes = _EVENT_WRITE

    @extend_schema(
        operation_id="ctf_events_list",
        parameters=[EventListQuerySerializer],
        responses=EventListResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        """Return the events the actor may administer (authority-aware, bounded).

        A platform administrator sees all live events; an ordinary organizer sees
        owned plus live staff-assigned events. Search/status/stable-owner/ordering
        are allowlisted data filters; the admin path is bounded to the canonical
        page size. The v1 ``{"events": [...]}`` envelope is unchanged (ADR-040).
        """
        from ctf.exceptions import CTFError
        from ctf.models import CTFEvent
        from shared.api.principals import authenticated_credential

        credential = authenticated_credential(request)
        query = EventListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        events = CTFEvent.objects.all()
        if params.get("status"):
            events = events.filter(status=params["status"])
        search = params.get("search")
        if search:
            events = events.filter(name__icontains=search)
        owner = params.get("owner_principal_uuid")
        if owner:
            events = events.filter(owner_principal_uuid=owner)
        ordering = params.get("ordering")
        if ordering:
            events = events.order_by(ordering, "id")

        try:
            rows = _authorized_event_page(
                events,
                credential,
                page=params.get("page"),
                page_size=params.get("page_size"),
            )
        except CTFError as exc:
            raise _CtfApiError(
                code="dependency_unavailable",
                message="Service unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        context = _event_projection_context(request, credential, rows)
        return Response({"events": EventSummarySerializer(rows, many=True, context=context).data})

    @extend_schema(request=EventWriteSerializer, responses={201: EventMutationResultSerializer})
    def post(self, request: Request) -> Response:
        """Create an event from the request body.

        The selected existing account parent is the authorization target. Human
        and service administrators use the same principal policy; creator
        attribution is separate from event ownership.
        """
        from django.core.exceptions import ValidationError

        from ctf.exceptions import CTFError, CTFPermissionError, CTFValidationError
        from ctf.services import create_event_for_credential
        from shared.api.principals import active_actor_user, authenticated_credential

        serializer = EventWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            try:
                event = create_event_for_credential(
                    authenticated_credential(request),
                    dict(serializer.validated_data),
                    creator_user=active_actor_user(request),
                )
            except CTFPermissionError:
                _raise_forbidden()
            except (CTFValidationError, ValidationError):
                _raise_invalid_event()
            except CTFError as exc:
                raise _CtfApiError(
                    code="dependency_unavailable",
                    message="Service unavailable",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                ) from exc
            return Response(
                {"id": str(event.id), "name": event.name, "status": event.status},
                status=status.HTTP_201_CREATED,
            )
        except _CtfApiError as exc:
            return exc.to_response(request)


class EventDetailView(APIView):
    """Get, update, or delete a single owned event."""

    parser_classes = [ModelSelectionJSONParser, FormParser, MultiPartParser]

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ
    required_write_scopes = _EVENT_WRITE

    @extend_schema(operation_id="ctf_events_retrieve", responses=EventDetailSerializer)
    def get(self, request: Request, event_id: UUID) -> Response:
        """Return the full event detail projection with owner and access context."""
        try:
            event = _resolve_owned_event(request, event_id, action="event.read")
        except _CtfApiError as exc:
            return exc.to_response(request)
        context = _event_projection_context(request, _credential(request), [event])
        return Response(EventDetailSerializer(event, context=context).data)

    @extend_schema(request=EventWriteSerializer, responses=EventMutationResultSerializer)
    def put(self, request: Request, event_id: UUID) -> Response:
        """Update mutable fields of an owned event."""
        from django.core.exceptions import ValidationError
        from django.db import transaction

        from ctf.exceptions import CTFStateError, CTFValidationError
        from ctf.services import update_event

        try:
            event = _resolve_owned_event(request, event_id, capability=EventCapability.CONFIG)
            source = _event_authority(request, event, EventCapability.CONFIG)
            serializer = EventWriteSerializer(data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            changed = sorted(serializer.validated_data.keys())
            try:
                # Database-only mutation: the update and its platform-admin
                # override audit share one transaction, so a strict audit failure
                # rolls the update back (ADR-052-R4). The service asserts the
                # config capability again (defense in depth, #1922).
                with transaction.atomic():
                    updated = update_event(event_id, dict(serializer.validated_data), actor_id=_credential(request))
                    _audit_admin_mutation(request, event, source, "event.update", changed_fields=changed)
            except (CTFValidationError, CTFStateError, ValidationError):
                _raise_invalid_event()
            return Response({"id": str(updated.id), "name": updated.name, "status": updated.status})
        except _CtfApiError as exc:
            return exc.to_response(request)

    @extend_schema(responses={204: None})
    def delete(self, request: Request, event_id: UUID) -> Response:
        """Soft-delete an owned event."""
        from django.db import transaction

        from ctf.services import delete_event

        try:
            event = _resolve_owned_event(request, event_id, capability=EventCapability.DELETE)
            source = _event_authority(request, event, EventCapability.DELETE)
        except _CtfApiError as exc:
            return exc.to_response(request)
        from shared.audit import AuditAction

        with transaction.atomic():
            delete_event(event_id, actor_id=_credential(request))
            _audit_admin_mutation(request, event, source, "event.delete", action=AuditAction.DELETE)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForceDeleteEventView(APIView):
    """Force-delete an event and tear down its ranges."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_write_scopes = _EVENT_WRITE

    @extend_schema(request=ForceDeleteEventRequestSerializer, responses=ForceDeleteEventResultSerializer)
    def post(self, request: Request, event_id: UUID) -> Response:
        """Validate the confirmation name and force-delete the event."""
        from ctf.exceptions import CTFValidationError
        from ctf.models import CTFEvent

        try:
            try:
                event = CTFEvent.all_objects.get(pk=event_id)
            except CTFEvent.DoesNotExist:
                _raise_not_found(_EVENT_NOT_FOUND)
            if not _actor_may_manage(request, event, None, action="event.delete", retained_delete=True):
                _raise_forbidden()
            confirmation_name = request.data.get("confirmation_name") if isinstance(request.data, dict) else None
            if not confirmation_name:
                _raise_bad_request("confirmation_name is required.")
            import ctf.services as ctf_services
            from shared.audit import AuditAction

            # Non-rollbackable range teardown: the context manager records bounded
            # override intent before the first side effect and a correlated
            # completed/failed outcome on EVERY exit path (ADR-052-R4).
            with admin_external_audit(request, "event.force_delete", action=AuditAction.DELETE):
                try:
                    result = ctf_services.force_delete_event(event_id, _credential(request), confirmation_name)
                except CTFValidationError:
                    _raise_invalid_event()
            return Response(result)
        except _CtfApiError as exc:
            return exc.to_response(request)


class ScenarioListView(APIView):
    """List CMS scenarios available for CTF events."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ

    @extend_schema(responses=CtfScenarioListResponseSerializer)
    def get(self, request: Request) -> Response:
        """Return scenario id/name pairs from the CMS registry."""
        import ctf.bridges as ctf_bridges

        scenarios = [{"id": sid, "name": name} for sid, name in ctf_bridges.cms_list_scenarios(_credential(request))]
        return Response({"scenarios": scenarios})
