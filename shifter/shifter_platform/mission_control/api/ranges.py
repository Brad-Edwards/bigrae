"""Range and catalog DRF views for Mission Control."""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from django.contrib.auth.models import User
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import (
    AccountScopeError,
    WorkspaceLaunchDenied,
    WorkspaceLaunchQuotaExceeded,
    get_active_range,
    get_mission_control_range_lease,
    has_mission_control_openvpn_profile,
    mission_control_range_scope,
)
from cms.services import (
    create_range_dispatch as cms_create_range,
)
from cms.services import (
    extend_mission_control_range as cms_extend_mission_control_range,
)
from cms.services import (
    get_agent as cms_get_agent,
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
from mission_control.api._base import (
    MissionControlAPIView,
    MissionControlReadAPIView,
    _range_write_permission,
    _raw_request,
    _validated,
)
from mission_control.api._retry_launch import LaunchChoices, RetrySafeLaunchMixin
from mission_control.api.permissions import HasMissionControlActor, block_participant_lifecycle_permission
from mission_control.api.rate_limit import RangeLaunchRateThrottle
from mission_control.api.serializers import (
    AgentListResponseSerializer,
    CurrentRangeResponseSerializer,
    LaunchRangeResponseSerializer,
    LaunchRangeSerializer,
    LaunchWorkspaceListResponseSerializer,
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
from shared.api.strict_json import ModelSelectionJSONParser
from shared.audit import AuditAction
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
        raise CMSError("Selected workspace is not available.") from exc


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
        _range_write_permission(),
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


class LaunchRangeView(RetrySafeLaunchMixin, MissionControlAPIView):
    """Launch a new cyber range."""

    parser_classes = [ModelSelectionJSONParser, FormParser, MultiPartParser]

    permission_classes = [
        IsAuthenticatedSessionOrApiToken,
        HasMissionControlActor,
        _range_write_permission(),
        block_participant_lifecycle_permission("launch"),
    ]
    # Backpressure (#322): per-actor + fleet admission budget, before CMS.
    throttle_classes = [RangeLaunchRateThrottle]

    @extend_schema(
        request=LaunchRangeSerializer,
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=False,
                description=(
                    "Optional caller retry key (max 200 characters; leading/trailing whitespace "
                    "trimmed, empty treated as absent). When supplied the launch is idempotent: a "
                    "retry with the same key and the same launch selections recovers the original "
                    "range instead of dispatching a duplicate; the same key with different "
                    "selections returns 409."
                ),
            )
        ],
        responses={
            200: LaunchRangeResponseSerializer,
            400: OpenApiResponse(ApiErrorSerializer, description="Request validation failed."),
            403: OpenApiResponse(ApiErrorSerializer, description="Workspace or range launch access denied."),
            409: OpenApiResponse(
                ApiErrorSerializer,
                description="Launch conflict, including workspace_range_quota_exceeded.",
            ),
            429: OpenApiResponse(ApiErrorSerializer, description="Request was throttled."),
        },
        operation_id="api_v1_mission_control_range_launch",
    )
    def post(self, request: Request) -> Response:
        """Validate input and create a range for the authenticated actor."""
        data, error = _validated(self, LaunchRangeSerializer, request.data)
        if error is not None:
            return error
        assert data is not None
        try:
            credential = authenticated_credential(request)
            selected_workspace = data.get("workspace_uuid")
            scope = mission_control_range_scope(credential.principal, selected_workspace)
        except (AccountScopeError, ValueError):
            return self.error_response(
                code="workspace_not_available",
                message="Selected workspace is not available.",
                status_code=403,
            )
        return self._dispatch_launch(request, self.actor_user(), credential, scope, data)

    def _launch_range(
        self,
        request: Request,
        user: User,
        credential: CredentialContext,
        scope: ResourceScope,
        data: dict[str, Any],
        caller_key: str | None = None,
    ) -> Response:
        """Launch a range once the request body has passed serializer checks."""
        scenario = str(data.get("scenario", "basic"))
        valid_scenarios = {s["id"] for s in cms_list_launchable_scenarios(credential, "range_launch", scope=scope)}
        if scenario not in valid_scenarios:
            return self.bad_request("Invalid scenario")

        agents_by_os, agents_error = self._resolve_agents_by_os(credential, data)
        if agents_error is not None:
            return agents_error

        return self._create_range(
            request,
            user,
            credential,
            scope,
            scenario,
            agents_by_os,
            caller_key,
            LaunchChoices(data.get("workspace_uuid"), self._agents_selection(data), data.get("model_sources")),
        )

    def _resolve_agents_by_os(
        self, credential: CredentialContext, data: dict[str, Any]
    ) -> tuple[dict[str, int] | None, Response | None]:
        """Resolve either the explicit agent map or a legacy single agent id."""
        agents_error: Response | None = None
        agents_by_os: dict[str, int] | None = None
        if "agents" in data:
            agents_by_os = cast(dict[str, int], data["agents"])
        else:
            agent_id = cast(int, data.get("agent_id"))
            try:
                agent = cms_get_agent(credential, agent_id)
            except CMSError as exc:
                logger.exception(
                    "Agent lookup failed: principal=%s agent_id=%s",
                    credential.principal.uuid,
                    safe_log_value(agent_id),
                )
                agents_error = self.bad_request(classify_user_message(str(exc), default="Agent not available"))
            else:
                os_type = "windows" if agent.os.slug == "windows" else "linux"
                agents_by_os = {os_type: agent_id}
        return agents_by_os, agents_error

    def _create_range(
        self,
        request: Request,
        user: User,
        credential: CredentialContext,
        scope: ResourceScope,
        scenario: str,
        agents_by_os: dict[str, int] | None,
        caller_key: str | None,
        choices: LaunchChoices,
    ) -> Response:
        """Create a range and record the launch audit event."""
        if caller_key is not None:
            return self._create_range_first_use(
                request, user, credential, scope, scenario, agents_by_os, caller_key, choices
            )
        source_kwargs: dict[str, Any] = {"model_sources": choices.model_sources} if choices.model_sources else {}
        try:
            range_ctx = cms_create_range(
                credential,
                scenario,
                workspace_uuid=choices.workspace_uuid,
                scope=scope,
                **source_kwargs,
            )
        except CMSError as exc:
            return self._launch_failure_response(exc, user, scenario)

        logger.info(
            "Range launched: user=%s request_id=%s agent=%s scenario=%s",
            safe_log_value(user.email),
            range_ctx.request_id,
            safe_log_value(range_ctx.agent_name),
            safe_log_value(scenario),
        )
        _audit_range_lifecycle(
            _raw_request(request),
            AuditAction.PROVISION,
            range_request_id=str(range_ctx.request_id),
            extra_state={"scenario": scenario, "agents": agents_by_os},
        )
        return Response({"success": True, "range": range_ctx.model_dump(mode="json")})

    def _launch_failure_response(self, exc: CMSError, user: User, scenario: str) -> Response:
        """Map a launch-time CMS failure to its bounded HTTP response.

        Kept distinct from the generic 400: an unavailable workspace scope is an
        opaque 403 (ADR-046-R9) and an enforcing concurrent-range quota is a 409
        Conflict (ADR-046-R10), never a 403 or a request-rate 429.
        """
        if isinstance(exc, WorkspaceLaunchDenied):
            logger.info("Range launch workspace denied: user=%s scenario=%s", user.pk, safe_log_value(scenario))
            return self.error_response(
                code="workspace_not_available",
                message="Selected workspace is not available.",
                status_code=403,
            )
        if isinstance(exc, WorkspaceLaunchQuotaExceeded):
            logger.info("Range launch quota exhausted: user=%s scenario=%s", user.pk, safe_log_value(scenario))
            return self.error_response(
                code="workspace_range_quota_exceeded",
                message="This workspace has reached its concurrent range limit.",
                status_code=409,
            )
        logger.exception("Range creation failed: user=%s scenario=%s", user.pk, safe_log_value(scenario))
        text = str(exc).lower()
        if "already have" in text or "active range" in text:
            response_msg = "You already have an active range"
        else:
            response_msg = classify_user_message(str(exc), default="Range could not be launched")
        return self.bad_request(response_msg)


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

        user = self.actor_user()
        credential = authenticated_credential(request)
        request_id = str(data["request_id"]) if data.get("request_id") else None
        range_id = data.get("range_id")
        try:
            if request_id:
                getattr(cms_services_mod, self.by_request_attr)(credential, request_id)
                logger.info(
                    "Range %s: user=%s request_id=%s",
                    self.log_verb,
                    safe_log_value(user.email),
                    safe_log_value(request_id),
                )
            else:
                getattr(cms_services_mod, self.by_id_attr)(credential, range_id)
                logger.info(
                    "Range %s: user=%s range_id=%s",
                    self.log_verb,
                    safe_log_value(user.email),
                    safe_log_value(range_id),
                )
        except CMSError as exc:
            logger.exception(
                "Range %s failed: user=%s request_id=%s range_id=%s",
                self.log_verb,
                user.pk,
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

    @extend_schema(
        parameters=[OpenApiParameter("workspace_uuid", type=str, required=False)],
        responses=AgentListResponseSerializer,
        operation_id="api_v1_mission_control_agents_list",
    )
    def get(self, request: Request) -> Response:
        """Return agents available to the authenticated actor."""
        credential = authenticated_credential(request)
        try:
            selected = _selected_workspace(request)
        except CMSError:
            return self.error_response(code="invalid", message="Invalid workspace.", status_code=400)
        try:
            scope = mission_control_range_scope(credential.principal, selected)
        except (AccountScopeError, ValueError):
            return self.error_response(
                code="workspace_not_available",
                message="Selected workspace is not available.",
                status_code=403,
            )
        return Response(
            {
                "agents": cms_list_agents(credential, scope=scope),
                "max_file_size_bytes": cms_max_agent_file_size_bytes(),
            }
        )


class LaunchWorkspaceListView(MissionControlReadAPIView):
    """Discover launchable workspaces independently of catalog selection."""

    @extend_schema(
        responses=LaunchWorkspaceListResponseSerializer,
        operation_id="api_v1_mission_control_launch_workspaces_list",
    )
    def get(self, request: Request) -> Response:
        """Return active workspaces admitted by exact launch-range policy."""
        from shared.authorization import configured_authorization_provider
        from workspaces.services import AccountScopeError, list_launch_workspace_options

        try:
            page = list_launch_workspace_options(
                authenticated_credential(request),
                configured_authorization_provider(),
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

    @extend_schema(
        parameters=[OpenApiParameter("workspace_uuid", type=str, required=False)],
        responses=ScenarioListResponseSerializer,
        operation_id="api_v1_mission_control_scenarios_list",
    )
    def get(self, request: Request) -> Response:
        """Return scenarios available to the authenticated actor."""
        credential = authenticated_credential(request)
        try:
            selected = _selected_workspace(request)
        except CMSError:
            return self.error_response(code="invalid", message="Invalid workspace.", status_code=400)
        try:
            scope = mission_control_range_scope(credential.principal, selected)
        except (AccountScopeError, ValueError):
            return self.error_response(
                code="workspace_not_available",
                message="Selected workspace is not available.",
                status_code=403,
            )
        scenarios = cms_list_launchable_scenarios(credential, "range_launch", scope=scope)
        return Response({"scenarios": scenarios})
