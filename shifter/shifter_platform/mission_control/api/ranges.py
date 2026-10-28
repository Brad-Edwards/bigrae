"""Range and catalog DRF views for Mission Control."""

from __future__ import annotations

import logging
from uuid import UUID

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import (
    AccountScopeError,
    get_active_range,
    get_mission_control_range_lease,
    has_mission_control_openvpn_profile,
    mission_control_range_scope,
)
from cms.services import (
    extend_mission_control_range as cms_extend_mission_control_range,
)
from cms.services import (
    list_agents as cms_list_agents,
)
from cms.services import (
    list_launchable_scenarios as cms_list_launchable_scenarios,
)
from cms.services import (
    max_agent_file_size_bytes as cms_max_agent_file_size_bytes,
)
from cms.services import (
    mission_control_launch_workspaces as cms_mission_control_launch_workspaces,
)
from mission_control.api._base import (
    MissionControlAPIView,
    MissionControlReadAPIView,
    _catalog_error,
    _raw_request,
    _validated,
)
from mission_control.api._range_launch import LaunchRangeView as LaunchRangeView
from mission_control.api.permissions import HasMissionControlActor, block_participant_lifecycle_permission
from mission_control.api.scope import WORKSPACE_UNAVAILABLE
from mission_control.api.serializers import (
    AgentListResponseSerializer,
    CurrentRangeResponseSerializer,
    LaunchWorkspaceListResponseSerializer,
    LaunchWorkspacePageQuerySerializer,
    RangeLeaseResponseSerializer,
    RangeLifecycleSerializer,
    ScenarioListResponseSerializer,
    SuccessResponseSerializer,
)
from mission_control.utils import build_connection_urls
from mission_control.views._common import _audit_range_lifecycle
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api.schema import ApiErrorSerializer
from shared.audit import AuditAction
from shared.authorization.operations import scope_actions
from shared.credentials import CredentialContext
from shared.errors import classify_user_message
from shared.exceptions import CMSError
from shared.identity_scope import ResourceScope
from shared.log_sanitize import safe_log_value
from shared.raes.presentation import build_range_participant_runtime_projection, build_range_raes_projection

logger = logging.getLogger(__name__)


def _selected_workspace(request: Request) -> UUID | None:
    """Parse the optional public workspace selector without accepting internal IDs."""
    value = request.query_params.get("workspace_uuid")
    try:
        return UUID(value) if value else None
    except ValueError as exc:
        raise CMSError(WORKSPACE_UNAVAILABLE) from exc


class CurrentRangeView(MissionControlReadAPIView):
    """Return the current user's active range."""

    @extend_schema(responses=CurrentRangeResponseSerializer, operation_id="api_v1_mission_control_range_retrieve")
    def get(self, request: Request) -> Response:
        """Return the active range and connection URLs for the request user."""
        credential = authenticated_credential(request)
        active_range = get_active_range(credential)
        if not active_range:
            return Response(
                {
                    "has_range": False,
                    "range": None,
                    "connection_urls": [],
                    "raes_projection": None,
                    "raes_participant_runtime": None,
                    "lifecycle": None,
                    "vpn_profile_available": False,
                }
            )
        projection = build_range_raes_projection(active_range.request_id)
        participant_runtime = build_range_participant_runtime_projection(
            active_range.request_id, active_range.instances
        )
        lease = get_mission_control_range_lease(credential)
        return Response(
            {
                "has_range": True,
                "range": active_range.model_dump(mode="json"),
                "connection_urls": build_connection_urls(active_range.instances),
                "raes_projection": projection.to_payload() if projection else None,
                "raes_participant_runtime": participant_runtime.to_payload() if participant_runtime else None,
                "lifecycle": lease.to_payload() if lease else None,
                "vpn_profile_available": has_mission_control_openvpn_profile(
                    credential, request_id=active_range.request_id
                ),
            }
        )


class ExtendRangeLeaseView(MissionControlAPIView):
    """Extend the authenticated actor's Mission Control range by one fixed increment."""

    permission_classes = [
        IsAuthenticatedSessionOrApiToken,
        HasMissionControlActor,
        block_participant_lifecycle_permission("extend"),
    ]

    @extend_schema(
        request=None,
        responses={
            200: RangeLeaseResponseSerializer,
            400: ApiErrorSerializer,
            404: ApiErrorSerializer,
            409: ApiErrorSerializer,
        },
    )
    def post(self, request: Request) -> Response:
        """Extend only the server-owned lease; caller timestamps are forbidden."""
        if request.body or request.query_params:
            response = self.error_response(
                code="invalid",
                message="Range extension requests must not include a body or query parameters.",
                status_code=400,
            )
        else:
            from cms.services import RangeLeaseConflict, RangeLeaseNotFound

            try:
                lease = cms_extend_mission_control_range(authenticated_credential(request))
            except RangeLeaseNotFound:
                response = self.not_found("Range not found")
            except RangeLeaseConflict:
                response = self.error_response(
                    code="range_extension_unavailable",
                    message="Range cannot be extended.",
                    status_code=409,
                )
            else:
                response = Response({"lifecycle": lease.to_payload()})
        return response


class RangeLifecycleView(MissionControlAPIView):
    """Base class for range lifecycle mutations."""

    log_verb = ""
    audit_action = ""
    by_request_attr = ""
    by_id_attr = ""
    lifecycle_verb = ""

    def get_permissions(self) -> list[object]:
        """Append the lifecycle-verb participant permission to the base gates."""
        permissions = super().get_permissions()
        permissions.append(block_participant_lifecycle_permission(self.lifecycle_verb)())
        return permissions

    def post(self, request: Request) -> Response:
        """Run the configured range lifecycle service method."""
        import cms.services as cms_services_mod

        data, error = _validated(self, RangeLifecycleSerializer, request.data)
        if error is not None:
            return error
        assert data is not None

        user = self.actor_credential()
        credential = authenticated_credential(request)
        request_id = str(data["request_id"]) if data.get("request_id") else None
        range_id = data.get("range_id")
        try:
            if request_id:
                getattr(cms_services_mod, self.by_request_attr)(credential, request_id)
                logger.info(
                    "Range %s: user=%s request_id=%s",
                    self.log_verb,
                    safe_log_value(str(user.principal.uuid)),
                    safe_log_value(request_id),
                )
            else:
                getattr(cms_services_mod, self.by_id_attr)(credential, range_id)
                logger.info(
                    "Range %s: user=%s range_id=%s",
                    self.log_verb,
                    safe_log_value(str(user.principal.uuid)),
                    safe_log_value(range_id),
                )
        except CMSError as exc:
            logger.exception(
                "Range %s failed: user=%s request_id=%s range_id=%s",
                self.log_verb,
                user.principal.uuid,
                safe_log_value(request_id),
                safe_log_value(range_id),
            )
            return self.bad_request(classify_user_message(str(exc), default="Range action could not be completed"))

        _audit_range_lifecycle(
            _raw_request(request),
            self.audit_action,
            range_id=range_id,
            range_request_id=request_id,
        )
        return Response({"success": True})


@extend_schema_view(
    post=extend_schema(
        request=RangeLifecycleSerializer,
        responses=SuccessResponseSerializer,
        operation_id="api_v1_mission_control_range_cancel",
    )
)
class CancelRangeView(RangeLifecycleView):
    """Cancel a pending or active range."""

    log_verb = "cancelled"
    audit_action = AuditAction.CANCEL
    by_request_attr = "cancel_range_by_request_id"
    by_id_attr = "cancel_range"
    lifecycle_verb = "cancel"


@extend_schema_view(
    post=extend_schema(
        request=RangeLifecycleSerializer,
        responses=SuccessResponseSerializer,
        operation_id="api_v1_mission_control_range_destroy",
    )
)
class DestroyRangeView(RangeLifecycleView):
    """Destroy a range."""

    log_verb = "destroyed"
    audit_action = AuditAction.DEPROVISION
    by_request_attr = "destroy_range_by_request_id"
    by_id_attr = "destroy_range"
    lifecycle_verb = "destroy"


@extend_schema_view(
    post=extend_schema(
        request=RangeLifecycleSerializer,
        responses=SuccessResponseSerializer,
        operation_id="api_v1_mission_control_range_pause",
    )
)
class PauseRangeView(RangeLifecycleView):
    """Pause a range."""

    log_verb = "paused"
    audit_action = AuditAction.PAUSE
    by_request_attr = "pause_range_by_request_id"
    by_id_attr = "pause_range"
    lifecycle_verb = "pause"


@extend_schema_view(
    post=extend_schema(
        request=RangeLifecycleSerializer,
        responses=SuccessResponseSerializer,
        operation_id="api_v1_mission_control_range_resume",
    )
)
class ResumeRangeView(RangeLifecycleView):
    """Resume a paused range."""

    log_verb = "resumed"
    audit_action = AuditAction.RESUME
    by_request_attr = "resume_range_by_request_id"
    by_id_attr = "resume_range"
    lifecycle_verb = "resume"


class AgentListView(MissionControlReadAPIView):
    """Return the authenticated user's agent list."""

    required_actions = scope_actions("read_content", installation=True)

    @extend_schema(
        parameters=[OpenApiParameter("workspace_uuid", type=str, required=False)],
        responses=AgentListResponseSerializer,
        operation_id="api_v1_mission_control_agents_list",
    )
    def get(self, request: Request) -> Response:
        """Return agents available to the authenticated actor."""
        credential = authenticated_credential(request)
        scope, error = _catalog_scope(self, request, credential)
        if error is not None:
            return error
        assert scope is not None
        try:
            agents = cms_list_agents(credential, scope=scope)
        except CMSError as exc:
            return _catalog_error(self, exc)
        return Response(
            {
                "agents": agents,
                "max_file_size_bytes": cms_max_agent_file_size_bytes(),
            }
        )


class LaunchWorkspaceListView(MissionControlReadAPIView):
    """Discover launchable workspaces independently of catalog selection."""

    required_actions = ("workspace.launch_range",)

    @extend_schema(
        parameters=[LaunchWorkspacePageQuerySerializer],
        responses=LaunchWorkspaceListResponseSerializer,
        operation_id="api_v1_mission_control_launch_workspaces_list",
    )
    def get(self, request: Request) -> Response:
        """Return active workspaces admitted by exact launch-range policy."""
        from shared.authorization import configured_authorization_provider

        query = LaunchWorkspacePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        try:
            page = cms_mission_control_launch_workspaces(
                authenticated_credential(request),
                configured_authorization_provider(),
                **query.validated_data,
            )
        except (AccountScopeError, ValueError):
            return self.error_response(
                code="workspace_discovery_unavailable",
                message="Launch workspaces are not available.",
                status_code=403,
            )
        return Response(
            {
                "count": page.count,
                "personal_available": True,
                "workspaces": [
                    {
                        "uuid": item.uuid,
                        "name": item.name,
                        "organization_uuid": item.organization_uuid,
                        "organization_name": item.organization_name,
                    }
                    for item in page.results
                ],
            }
        )


class ScenarioListView(MissionControlReadAPIView):
    """Return available range scenarios."""

    required_actions = scope_actions("read_content", installation=True)

    @extend_schema(
        parameters=[OpenApiParameter("workspace_uuid", type=str, required=False)],
        responses=ScenarioListResponseSerializer,
        operation_id="api_v1_mission_control_scenarios_list",
    )
    def get(self, request: Request) -> Response:
        """Return scenarios available to the authenticated actor."""
        credential = authenticated_credential(request)
        scope, error = _catalog_scope(self, request, credential)
        if error is not None:
            return error
        assert scope is not None
        try:
            scenarios = cms_list_launchable_scenarios(credential, "range_launch", scope=scope)
        except CMSError as exc:
            return _catalog_error(self, exc)
        return Response({"scenarios": scenarios})


def _catalog_scope(
    view: MissionControlAPIView, request: Request, credential: CredentialContext
) -> tuple[ResourceScope | None, Response | None]:
    """Keep malformed selectors distinct from unavailable native placement before catalog reads."""
    try:
        selected = _selected_workspace(request)
    except CMSError:
        return None, view.error_response(code="invalid", message="Invalid workspace.", status_code=400)
    try:
        scope = mission_control_range_scope(credential.principal, selected)
    except (AccountScopeError, ValueError):
        return None, view.error_response(
            code="workspace_not_available",
            message=WORKSPACE_UNAVAILABLE,
            status_code=403,
        )
    return scope, None
