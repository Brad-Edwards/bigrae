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
    CtfScenarioListQuerySerializer,
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
    from django.db.models import QuerySet

    from shared.credentials import CredentialContext

_SERVICE_UNAVAILABLE = "Service unavailable"

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any
    from uuid import UUID

    from ctf.models import CTFEvent
    from shared.authorization import AuthorizationRequest

# Bound both policy discovery and serialized pages. Counts cover every readable
# candidate, not merely the current page (ADR-052-R3, ADR-040).
_EVENT_LIST_PAGE_SIZE = 200
_EVENT_LIST_CANDIDATE_LIMIT = 1000


def _authorized_event_page(
    events: QuerySet[CTFEvent], credential: CredentialContext, *, page: int | None, page_size: int | None
) -> tuple[list[CTFEvent], dict[str, int | None]]:
    """Filter a finite candidate set before paging; broad discovery must be refined."""
    from ctf.exceptions import CTFPermissionError, CTFValidationError
    from ctf.services.unified_authorization import require_event_action

    size = page_size or _EVENT_LIST_PAGE_SIZE
    number = page or 1
    start = (number - 1) * size
    stop = start + size
    readable: list[CTFEvent] = []
    candidates = list(events[: _EVENT_LIST_CANDIDATE_LIMIT + 1])
    if len(candidates) > _EVENT_LIST_CANDIDATE_LIMIT:
        raise CTFValidationError("Refine event filters", code="CTF_EVENT_DISCOVERY_LIMIT")
    for event in candidates:
        try:
            require_event_action(credential, event.pk, "event.read")
        except CTFPermissionError:
            continue
        readable.append(event)
    return readable[start:stop], {
        "count": len(readable),
        "active_count": sum(event.status in {"registration", "active", "paused"} for event in readable),
        "team_mode_count": sum(event.team_mode for event in readable),
        "page": number,
        "page_size": size,
        "next_page": number + 1 if stop < len(readable) else None,
    }


def _event_projection_context(
    request: Request, credential: CredentialContext, events: Sequence[CTFEvent]
) -> dict[str, Any]:
    """Build advisory capabilities from the same live decisions as mutations."""
    from shared.authorization import configured_authorization_provider

    context: dict[str, Any] = {"request": request, "access_capabilities": {}}
    indexed, checks = _projection_checks(credential, events)
    if not checks:
        return context
    try:
        decisions = configured_authorization_provider().batch_check(tuple(checks))
    except Exception:
        decisions = ()
    if len(decisions) != len(indexed) or any(decision.kind == "evaluator_error" for decision in decisions):
        return context
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

        Every row and aggregate count uses the same live event-read policy.
        Search/status/stable-owner/ordering are allowlisted data filters. Bounded
        pages include continuation and counts for the complete readable result.
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
        events = events.order_by(params.get("ordering") or "-event_start", "id")

        try:
            rows, pagination = _authorized_event_page(
                events,
                credential,
                page=params.get("page"),
                page_size=params.get("page_size"),
            )
        except CTFError as exc:
            if exc.code == "CTF_EVENT_DISCOVERY_LIMIT":
                return _CtfApiError(
                    code="event_discovery_limit", message="Refine event filters.", status_code=400
                ).to_response(request)
            return _CtfApiError(
                code="dependency_unavailable",
                message=_SERVICE_UNAVAILABLE,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ).to_response(request)
        context = _event_projection_context(request, credential, rows)
        return Response({"events": EventSummarySerializer(rows, many=True, context=context).data, **pagination})

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
                    message=_SERVICE_UNAVAILABLE,
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
            source = _event_authority()
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
            source = _event_authority()
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
        from ctf.exceptions import CTFStateError, CTFValidationError
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
                except CTFStateError:
                    raise _CtfApiError(
                        status_code=503, code="event_cleanup_unavailable", message="Event range cleanup is unavailable."
                    ) from None
            return Response(result)
        except _CtfApiError as exc:
            return exc.to_response(request)


class ScenarioListView(APIView):
    """List CMS scenarios available for CTF events."""

    permission_classes = CTF_ORGANIZER_PERMISSIONS
    required_read_scopes = _EVENT_READ

    @extend_schema(
        operation_id="ctf_scenario_options_retrieve",
        parameters=[CtfScenarioListQuerySerializer],
        responses=CtfScenarioListResponseSerializer,
    )
    def get(self, request: Request) -> Response:
        """Return scenario id/name pairs from the CMS registry."""
        from rest_framework.exceptions import ValidationError

        import ctf.bridges as ctf_bridges
        from shared.exceptions import CMSError
        from shared.identity_scope import ResourceScope

        if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
            raise ValidationError("Invalid request")
        query = CtfScenarioListQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        data = query.validated_data
        scope = ResourceScope(data["scope_kind"], data.get("account"), data.get("organization"), data.get("workspace"))
        try:
            scenarios = [
                {"id": sid, "name": name}
                for sid, name in ctf_bridges.cms_list_scenarios(_credential(request), scope=scope)
            ]
        except CMSError as exc:
            unavailable = exc.details.get("code") == "authority-unavailable"
            return _CtfApiError(
                code="dependency_unavailable" if unavailable else "forbidden",
                message=_SERVICE_UNAVAILABLE if unavailable else "Permission denied",
                status_code=503 if unavailable else 403,
            ).to_response(request)
        return Response({"scenarios": scenarios})


def _projection_checks(
    credential: CredentialContext,
    events: Sequence[CTFEvent],
) -> tuple[list[tuple[UUID, str]], list[AuthorizationRequest]]:
    """Build only concrete event actions permitted by the current credential ceiling."""
    from ctf.services.credential_scope import event_credential_scope
    from ctf.services.event.staff import capability_action_pairs
    from shared.authorization import AuthorizationRequest, TargetRef

    indexed: list[tuple[UUID, str]] = []
    checks: list[AuthorizationRequest] = []
    for event in events:
        scope = event_credential_scope(event.pk)
        target = TargetRef("event", event.pk)
        for capability, action in capability_action_pairs():
            if not credential.ceiling.permits(action, target):
                continue
            indexed.append((event.pk, capability))
            checks.append(
                AuthorizationRequest(
                    credential.principal,
                    action,
                    target,
                    scope,
                    credential.ceiling,
                )
            )
    return indexed, checks
