"""Mission Control range-history API projection."""

from __future__ import annotations

from uuid import UUID

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import AccountScopeError, list_mission_control_range_history, mission_control_range_scope
from mission_control.api._base import MissionControlReadAPIView
from mission_control.api.serializers import RangeHistoryResponseSerializer, RangeHistorySerializer
from shared.api.principals import authenticated_credential


class RangeHistoryView(MissionControlReadAPIView):
    """Return the authenticated user's range history (#1370).

    The product-scoped query includes soft-deleted terminal ranges and excludes
    CTF-sourced ranges. Raw rows are projected explicitly because history has no
    hydrated instances, agent name, or computed-status fields.
    """

    @extend_schema(
        parameters=[OpenApiParameter("workspace_uuid", type=str, required=False)],
        responses=RangeHistoryResponseSerializer,
        operation_id="api_v1_mission_control_ranges_list",
    )
    def get(self, request: Request) -> Response:
        """Return the authenticated actor's Mission Control range history, newest first."""
        credential = authenticated_credential(request)
        workspace_value = request.query_params.get("workspace_uuid")
        try:
            workspace_uuid = UUID(workspace_value) if workspace_value else None
        except ValueError:
            return self.error_response(code="invalid", message="Invalid workspace.", status_code=400)
        try:
            scope = mission_control_range_scope(credential.principal, workspace_uuid) if workspace_uuid else None
        except (AccountScopeError, ValueError):
            return self.error_response(
                code="workspace_not_available",
                message="Selected workspace is not available.",
                status_code=403,
            )
        ranges = list_mission_control_range_history(credential, scope=scope)
        serializer = RangeHistorySerializer(
            [
                {
                    "request_id": range_instance.request.request_id if range_instance.request else None,
                    "range_id": range_instance.range_id,
                    "scenario_id": range_instance.scenario_id,
                    "status": range_instance.status,
                    "range_source": range_instance.range_source,
                    "created_at": range_instance.created_at,
                    "updated_at": range_instance.updated_at,
                    "deleted_at": range_instance.deleted_at,
                }
                for range_instance in ranges
            ],
            many=True,
        )
        return Response({"ranges": serializer.data})
