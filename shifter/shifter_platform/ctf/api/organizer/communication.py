"""Scoped communication REST controllers; services own live policy and writes."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

import workspaces.services as ws
from ctf.api._base import HasActiveCTFActor, HasCTFEventAdminAccess, ctf_error_response
from ctf.api.communication_schema import CommunicationSchema
from ctf.api.serializers.communication import (
    CommunicationCampaignSummarySerializer,
    CommunicationCreateSerializer,
    CommunicationEmptySerializer,
    CommunicationIntentSerializer,
    CommunicationJSONParser,
    CommunicationReleaseSerializer,
    CommunicationRevisionSerializer,
    with_latest_revision,
)
from ctf.api.serializers.communication_console import (
    CommunicationActivitySerializer,
    CommunicationDeliveryReportSerializer,
    CommunicationPageQuerySerializer,
    CommunicationPreviewQuerySerializer,
    CommunicationPreviewSerializer,
    CommunicationRecipientPageSerializer,
    CommunicationRecipientQuerySerializer,
    CommunicationTargetOptionsQuerySerializer,
    CommunicationTargetOptionsSerializer,
    CommunicationTargetQuerySerializer,
    CommunicationTargetsSerializer,
)
from ctf.exceptions import CTFCommunicationError, CTFError
from ctf.models import CommunicationCampaign, MessageRevision
from ctf.services.communication import (
    AdmissionActor,
    CampaignDraft,
    cancel_campaign,
    create_campaign,
    release_campaign,
    reporting,
    revise_message,
    schedule_declaration,
)
from ctf.services.communication.read_admission import authorized_campaign
from shared.api.closed_serializer import ClosedSerializer
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api_tokens.permissions import require_scope
from shared.api_tokens.scopes import CTF_COMMUNICATION_READ, CTF_COMMUNICATION_WRITE


class CampaignQuerySerializer(ClosedSerializer):
    """Finite, deterministic workspace collections."""

    account = serializers.UUIDField(required=False)
    organization = serializers.UUIDField(required=False)
    workspace = serializers.UUIDField(required=False)
    event_id = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=100, default=25)
    offset = serializers.IntegerField(min_value=0, max_value=100000, default=0)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Require exactly one account, organization or workspace parent selector."""
        if sum(name in attrs for name in ("account", "organization", "workspace")) != 1:
            raise serializers.ValidationError("Invalid value.")
        return attrs


class CampaignListSerializer(serializers.Serializer):
    """A bounded page; offset advances across authorized workspace candidates."""

    results = CommunicationCampaignSummarySerializer(many=True)
    next_offset = serializers.IntegerField(allow_null=True)


def request_actor(request: Request) -> AdmissionActor:
    """Use the canonical principal credential independently of request.user."""
    from shared.audit import get_request_id

    return AdmissionActor(credential=authenticated_credential(request), request_id=get_request_id(request))


def validate_query(request: Request, serializer_class: type[ClosedSerializer]) -> dict[str, Any]:
    """Reject unknown or repeated query parameters before validation."""

    if any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise serializers.ValidationError("Invalid value.")
    serializer = serializer_class(data=request.query_params.dict())
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class CommunicationView(APIView):
    """Bearer-first auth, closed JSON and canonical domain error translation."""

    schema = CommunicationSchema()
    # Admit current non-temporary credentials. Owning services recheck exact
    # native policy on every command/read; event ownership alone grants no access.
    permission_classes = [
        IsAuthenticatedSessionOrApiToken,
        HasActiveCTFActor,
        HasCTFEventAdminAccess,
        require_scope(CTF_COMMUNICATION_READ, CTF_COMMUNICATION_WRITE),
    ]
    parser_classes = [CommunicationJSONParser]

    def handle_exception(self, exc: Exception) -> Response:
        """Map known communication failures to bounded public error responses."""
        if isinstance(exc, CTFError):
            return ctf_error_response(self.request, exc)
        if isinstance(exc, serializers.ValidationError):
            from shared.api.errors import api_error_response

            return api_error_response(code="invalid", message="Invalid request", status_code=400, request=self.request)
        return super().handle_exception(exc)

    def campaign(self, request: Request, campaign_id: UUID) -> CommunicationCampaign:
        """Resolve the exact campaign under current read or write admission."""
        required_scope = CTF_COMMUNICATION_READ if request.method == "GET" else CTF_COMMUNICATION_WRITE
        with authorized_campaign(campaign_id, request_actor(request), required_scope=required_scope) as campaign:
            return campaign

    def input(self, request: Request, serializer_class: type[ClosedSerializer]) -> dict[str, Any]:
        """Reject extra query fields and validate the closed command body."""
        validate_query(request, CommunicationEmptySerializer)
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class CommunicationListView(CommunicationView):
    """Create or list campaigns within one explicit native parent."""

    @extend_schema(
        operation_id="ctf_communications_list", parameters=[CampaignQuerySerializer], responses=CampaignListSerializer
    )
    def get(self, request: Request) -> Response:
        """Page authorized campaigns beneath one explicit native parent."""
        query = validate_query(request, CampaignQuerySerializer)
        parent_type = next(name for name in ("account", "organization", "workspace") if name in query)
        try:
            scope = ws.event_placement_scope(parent_type, query[parent_type])
            binding = ws.resolve_resource_scope(scope)
        except ws.AccountScopeError:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_WORKSPACE_DENIED") from None
        from ctf.services.communication.admission import prepare_collection_actor

        collection_actor = prepare_collection_actor(request_actor(request), required_scope=CTF_COMMUNICATION_READ)
        start, limit = query["offset"], query["limit"]
        queryset = CommunicationCampaign.objects.filter(
            scope_kind=scope.kind,
            account_id=binding.account_id,
            organization_id=binding.organization_id,
            workspace_id=binding.workspace_id,
        )
        if "event_id" in query:
            queryset = queryset.filter(target_events__pk=query["event_id"])
        candidates = list(
            with_latest_revision(queryset).prefetch_related("target_events").order_by("created_at", "pk")[:10001]
        )
        if len(candidates) > 10000:
            raise CTFCommunicationError(
                "Communication collection is too large", code="CTF_COMMUNICATION_COLLECTION_BOUNDED"
            )
        visible = []
        for campaign in candidates:
            try:
                with authorized_campaign(campaign.pk, collection_actor):
                    # Keep the collection's bounded, body-free revision annotation.
                    # The owning read admission independently fences fresh target rows.
                    visible.append(campaign)
            except CTFCommunicationError as exc:
                if exc.code in {"CTF_COMMUNICATION_EVENT_DENIED", "CTF_COMMUNICATION_TARGET_DENIED"}:
                    continue
                raise
        page = visible[start : start + limit]
        return Response(
            {
                "results": CommunicationCampaignSummarySerializer(page, many=True).data,
                "next_offset": start + limit if len(visible) > start + limit else None,
            }
        )

    @extend_schema(request=CommunicationCreateSerializer, responses={201: CommunicationCampaignSummarySerializer})
    def post(self, request: Request) -> Response:
        """Create an immutable draft under the selected parent and live request proof."""
        data = self.input(request, CommunicationCreateSerializer)
        parent_type = next(name for name in ("account", "organization", "workspace") if name in data)
        parent_uuid = data.pop(parent_type)
        actor = request_actor(request)
        campaign = create_campaign(
            authenticated_credential(request),
            parent_uuid,
            CampaignDraft(**data, origin="organizer_staff", actor_token_id=actor.token_id),
            actor=actor,
            parent_type=parent_type,
        )
        return Response(CommunicationCampaignSummarySerializer(campaign).data, status=201)


class CommunicationDetailView(CommunicationView):
    """Project a campaign only after complete-target authorization."""

    @extend_schema(responses=CommunicationCampaignSummarySerializer)
    def get(self, request: Request, campaign_id: UUID) -> Response:
        """Read the campaign projection after complete current target admission."""
        validate_query(request, CommunicationEmptySerializer)
        return Response(CommunicationCampaignSummarySerializer(self.campaign(request, campaign_id)).data)


class CommunicationRevisionView(CommunicationView):
    """Append validated content to an authorized draft campaign."""

    @extend_schema(request=CommunicationRevisionSerializer, responses={201: CommunicationCampaignSummarySerializer})
    def post(self, request: Request, campaign_id: UUID) -> Response:
        """Append a validated message revision to the admitted campaign."""
        data = self.input(request, CommunicationRevisionSerializer)
        campaign = self.campaign(request, campaign_id)
        revise_message(campaign, actor=request_actor(request), **data)
        return Response(CommunicationCampaignSummarySerializer(campaign).data, status=201)


class CommunicationReleaseView(CommunicationView):
    """Accept a release or schedule declaration through the ledger."""

    @extend_schema(request=CommunicationReleaseSerializer, responses={202: CommunicationIntentSerializer})
    def post(self, request: Request, campaign_id: UUID) -> Response:
        """Release a reviewed revision now or retain its scheduled declaration."""
        data = self.input(request, CommunicationReleaseSerializer)
        campaign = self.campaign(request, campaign_id)
        revision = None
        if "revision_id" in data:
            revision = MessageRevision.objects.filter(pk=data["revision_id"], campaign=campaign).first()
            if revision is None:
                raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_NOT_FOUND")
        from ctf.services.communication.adapters import registered_channels

        if set(campaign.channels) - set(registered_channels()):
            raise CTFCommunicationError("Channel unavailable", code="CTF_COMMUNICATION_CHANNEL_UNAVAILABLE")
        kwargs = {"occurrence_key": data["occurrence_key"], "revision": revision}
        actor = request_actor(request)
        if campaign.trigger_spec["kind"] == "absolute_time":
            intent = schedule_declaration(
                campaign, due_at=datetime.fromisoformat(campaign.trigger_spec["due_at"]), actor=actor, **kwargs
            )
        else:
            intent = release_campaign(campaign, admission=actor, **kwargs)
        return Response(CommunicationIntentSerializer(intent).data, status=202)


class CommunicationCancelView(CommunicationView):
    """Cancel unclaimed work for an authorized campaign."""

    @extend_schema(request=CommunicationEmptySerializer, responses=CommunicationCampaignSummarySerializer)
    def post(self, request: Request, campaign_id: UUID) -> Response:
        """Cancel unclaimed campaign work under current communication authority."""
        self.input(request, CommunicationEmptySerializer)
        campaign = self.campaign(request, campaign_id)
        cancel_campaign(campaign, actor=request_actor(request))
        campaign.refresh_from_db()
        return Response(CommunicationCampaignSummarySerializer(campaign).data)


class CommunicationTargetsView(CommunicationView):
    """List the workspaces and events where the actor may author a communication."""

    @extend_schema(
        operation_id="ctf_communications_targets_list",
        parameters=[CommunicationTargetQuerySerializer],
        responses=CommunicationTargetsSerializer,
    )
    def get(self, request: Request) -> Response:
        """Project authorized workspace targets through the shared discovery helper."""
        query = validate_query(request, CommunicationTargetQuerySerializer)
        data = reporting.list_targets(
            request_actor(request),
            workspace_uuid=query.get("workspace_id"),
            limit=query["limit"],
            offset=query["offset"],
        )
        return Response(CommunicationTargetsSerializer(data).data)


class CommunicationTargetOptionsView(CommunicationView):
    """Discover native direct campaign parents through exact event authority."""

    @extend_schema(
        operation_id="ctf_communications_target_options_list",
        parameters=[CommunicationTargetOptionsQuerySerializer],
        responses=CommunicationTargetOptionsSerializer,
    )
    def get(self, request: Request) -> Response:
        """List current communication parents and events under the request credential."""
        query = validate_query(request, CommunicationTargetOptionsQuerySerializer)
        parent_type = next((name for name in ("account", "organization", "workspace") if name in query), None)
        data = reporting.list_target_options(
            request_actor(request),
            parent_type=parent_type,
            parent_uuid=query[parent_type] if parent_type is not None else None,
            limit=query["limit"],
            offset=query["offset"],
        )
        return Response(CommunicationTargetOptionsSerializer(data).data)


class CommunicationPreviewView(CommunicationView):
    """Project a reviewed revision and the current audience estimate; writes nothing."""

    @extend_schema(
        operation_id="ctf_communications_preview_retrieve",
        parameters=[CommunicationPreviewQuerySerializer],
        responses=CommunicationPreviewSerializer,
    )
    def get(self, request: Request, campaign_id: UUID) -> Response:
        """Preview the reviewed revision and current audience without creating delivery work."""
        query = validate_query(request, CommunicationPreviewQuerySerializer)
        data = reporting.preview(campaign_id, query["revision_id"], actor=request_actor(request))
        return Response(CommunicationPreviewSerializer(data).data)


class CommunicationDeliveryView(CommunicationView):
    """Aggregate delivery state per occurrence, event, and channel."""

    @extend_schema(
        operation_id="ctf_communications_delivery_retrieve",
        parameters=[CommunicationPageQuerySerializer],
        responses=CommunicationDeliveryReportSerializer,
    )
    def get(self, request: Request, campaign_id: UUID) -> Response:
        """Page admitted delivery state grouped by occurrence, event and channel."""
        query = validate_query(request, CommunicationPageQuerySerializer)
        data = reporting.delivery_report(
            campaign_id, actor=request_actor(request), limit=query["limit"], offset=query["offset"]
        )
        return Response(CommunicationDeliveryReportSerializer(data).data)


class CommunicationRecipientsView(CommunicationView):
    """Minimal recipient rows for one occurrence and one target event."""

    @extend_schema(
        operation_id="ctf_communications_recipients_list",
        parameters=[CommunicationRecipientQuerySerializer],
        responses=CommunicationRecipientPageSerializer,
    )
    def get(self, request: Request, campaign_id: UUID) -> Response:
        """Page minimal recipient state for one admitted occurrence and target event."""
        query = validate_query(request, CommunicationRecipientQuerySerializer)
        data = reporting.recipient_rows(
            campaign_id,
            actor=request_actor(request),
            intent_id=query["intent_id"],
            event_id=query["event_id"],
            limit=query["limit"],
            offset=query["offset"],
        )
        return Response(CommunicationRecipientPageSerializer(data).data)


class CommunicationActivityView(CommunicationView):
    """The body-free campaign timeline from ledger attribution."""

    @extend_schema(operation_id="ctf_communications_activity_retrieve", responses=CommunicationActivitySerializer)
    def get(self, request: Request, campaign_id: UUID) -> Response:
        """Return the body-free campaign timeline under current target authority."""
        validate_query(request, CommunicationEmptySerializer)
        data = reporting.activity_timeline(campaign_id, actor=request_actor(request))
        return Response(CommunicationActivitySerializer(data).data)
