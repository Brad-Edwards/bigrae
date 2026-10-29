"""Native range launch and bounded failure projection for Mission Control."""

from __future__ import annotations

import logging
from typing import Any, cast

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import (
    AccountScopeError,
    WorkspaceLaunchDenied,
    WorkspaceLaunchQuotaExceeded,
    mission_control_range_scope,
)
from cms.services import (
    create_range_dispatch as cms_create_range,
)
from cms.services import (
    get_agent as cms_get_agent,
)
from cms.services import (
    list_launchable_scenarios as cms_list_launchable_scenarios,
)
from mission_control.api._base import (
    MissionControlAPIView,
    _catalog_error,
    _raw_request,
    _validated,
)
from mission_control.api._retry_launch import LaunchChoices, RetrySafeLaunchMixin
from mission_control.api.permissions import HasMissionControlActor, block_participant_lifecycle_permission
from mission_control.api.rate_limit import RangeLaunchRateThrottle
from mission_control.api.scope import WORKSPACE_UNAVAILABLE
from mission_control.api.serializers import (
    LaunchRangeResponseSerializer,
    LaunchRangeSerializer,
)
from mission_control.views._common import _audit_range_lifecycle
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api.schema import ApiErrorSerializer
from shared.api.strict_json import ModelSelectionJSONParser
from shared.audit import AuditAction
from shared.authorization.operations import scope_actions
from shared.credentials import CredentialContext
from shared.errors import classify_user_message
from shared.exceptions import CMSError
from shared.identity_scope import ResourceScope
from shared.log_sanitize import safe_log_value

logger = logging.getLogger(__name__)


class LaunchRangeView(RetrySafeLaunchMixin, MissionControlAPIView):
    """Launch a new cyber range."""

    required_actions = scope_actions("launch_range")

    parser_classes = [ModelSelectionJSONParser, FormParser, MultiPartParser]

    permission_classes = [
        IsAuthenticatedSessionOrApiToken,
        HasMissionControlActor,
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
                message=WORKSPACE_UNAVAILABLE,
                status_code=403,
            )
        return self._dispatch_launch(request, self.actor_credential(), credential, scope, data)

    def _launch_range(
        self,
        request: Request,
        user: CredentialContext,
        credential: CredentialContext,
        scope: ResourceScope,
        data: dict[str, Any],
        caller_key: str | None = None,
    ) -> Response:
        """Launch a range once the request body has passed serializer checks."""
        scenario = str(data.get("scenario", "basic"))
        scenario_error = self._validate_launch_scenario(credential, scope, scenario)
        if scenario_error is not None:
            return scenario_error

        agents_by_os, agents_error = self._resolve_agents_by_os(credential, data)
        if agents_error is not None:
            return agents_error

        return self._create_range(
            request,
            user,
            credential,
            scope,
            scenario,
            caller_key,
            LaunchChoices(
                data.get("workspace_uuid"), self._agents_selection(data), data.get("model_sources"), agents_by_os
            ),
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
        user: CredentialContext,
        credential: CredentialContext,
        scope: ResourceScope,
        scenario: str,
        caller_key: str | None,
        choices: LaunchChoices,
    ) -> Response:
        """Create a range and record the launch audit event."""
        if caller_key is not None:
            return self._create_range_first_use(request, user, credential, scope, scenario, caller_key, choices)
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
            safe_log_value(str(user.principal.uuid)),
            range_ctx.request_id,
            safe_log_value(range_ctx.agent_name),
            safe_log_value(scenario),
        )
        _audit_range_lifecycle(
            _raw_request(request),
            AuditAction.PROVISION,
            range_request_id=str(range_ctx.request_id),
            extra_state={"scenario": scenario, "agents": choices.agents_by_os},
        )
        return Response({"success": True, "range": range_ctx.model_dump(mode="json")})

    def _launch_failure_response(self, exc: CMSError, user: CredentialContext, scenario: str) -> Response:
        """Map a launch-time CMS failure to its bounded HTTP response.

        Kept distinct from the generic 400: an unavailable workspace scope is an
        opaque 403 (ADR-046-R9) and an enforcing concurrent-range quota is a 409
        Conflict (ADR-046-R10), never a 403 or a request-rate 429.
        """
        if isinstance(exc, WorkspaceLaunchDenied):
            logger.info(
                "Range launch workspace denied: user=%s scenario=%s", user.principal.uuid, safe_log_value(scenario)
            )
            return self.error_response(
                code="workspace_not_available",
                message=WORKSPACE_UNAVAILABLE,
                status_code=403,
            )
        if isinstance(exc, WorkspaceLaunchQuotaExceeded):
            logger.info(
                "Range launch quota exhausted: user=%s scenario=%s", user.principal.uuid, safe_log_value(scenario)
            )
            return self.error_response(
                code="workspace_range_quota_exceeded",
                message="This workspace has reached its concurrent range limit.",
                status_code=409,
            )
        logger.exception("Range creation failed: user=%s scenario=%s", user.principal.uuid, safe_log_value(scenario))
        text = str(exc).lower()
        if "already have" in text or "active range" in text:
            response_msg = "You already have an active range"
        else:
            response_msg = classify_user_message(str(exc), default="Range could not be launched")
        return self.bad_request(response_msg)

    def _validate_launch_scenario(
        self, credential: CredentialContext, scope: ResourceScope, scenario: str
    ) -> Response | None:
        """Admit the selected catalog source before resolving agents or reserving a launch."""
        try:
            valid_scenarios = {s["id"] for s in cms_list_launchable_scenarios(credential, "range_launch", scope=scope)}
        except CMSError as exc:
            return _catalog_error(self, exc)
        if scenario not in valid_scenarios:
            return self.bad_request("Invalid scenario")
        return None
