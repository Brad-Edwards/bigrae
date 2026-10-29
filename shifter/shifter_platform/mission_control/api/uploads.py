"""Upload DRF views for Mission Control."""

from __future__ import annotations

import logging
import os
from typing import Any

from django.contrib.sessions.backends.base import SessionBase
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ParseError
from rest_framework.request import Request
from rest_framework.response import Response

from cms.services import cancel_upload as cms_cancel_upload
from cms.services import complete_upload as cms_complete_upload
from cms.services import initiate_upload as cms_initiate_upload
from mission_control.api._base import MissionControlAPIView, _validated
from mission_control.api.permissions import HasMissionControlActor
from mission_control.api.scope import resource_placement
from mission_control.api.serializers import SuccessResponseSerializer
from mission_control.api.upload_serializers import (
    UploadCancelSerializer,
    UploadCompleteResponseSerializer,
    UploadCompleteSerializer,
    UploadInitiateResponseSerializer,
    UploadInitiateSerializer,
)
from mission_control.upload_session import check_upload_in_progress, set_upload_in_progress, upload_lock_matches_token
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.schema import ApiErrorSerializer
from shared.authorization.operations import scope_actions
from shared.credentials import CredentialContext
from shared.errors import classify_user_message
from shared.exceptions import CMSError
from shared.log_sanitize import safe_log_value

logger = logging.getLogger(__name__)


def _upload_session(request: Request) -> SessionBase | dict[str, Any]:
    """Browser presentation state never supplies token/service authority."""
    from shared.api.principals import authenticated_credential

    return request.session if authenticated_credential(request).kind == "session" else {}


def _clear_matching_upload(request: Request, token: str) -> None:
    """A response for an older operation cannot clear another upload's UI marker."""
    session = _upload_session(request)
    if upload_lock_matches_token(session, token):
        set_upload_in_progress(session, False)


class UploadInitiateView(MissionControlAPIView):
    """Initiate a presigned agent upload."""

    required_actions = scope_actions("manage_content")

    permission_classes = [IsAuthenticatedSessionOrApiToken, HasMissionControlActor]

    @extend_schema(
        request=UploadInitiateSerializer,
        responses={200: UploadInitiateResponseSerializer, 400: ApiErrorSerializer, 409: ApiErrorSerializer},
        operation_id="api_v1_mission_control_upload_initiate",
    )
    def post(self, request: Request) -> Response:
        """Create an upload session and presigned destination for an agent."""
        if check_upload_in_progress(_upload_session(request)):
            return self.error_response(
                code="conflict",
                message="An upload is already in progress. Please wait for it to complete.",
                status_code=409,
            )

        data, error = _validated(self, UploadInitiateSerializer, request.data)
        if error is not None:
            return error
        assert data is not None

        user = self.actor_credential()
        return self._initiate_upload(request, user, data)

    def _initiate_upload(self, request: Request, user: CredentialContext, data: dict[str, Any]) -> Response:
        """Create the presigned upload and mark the session upload state."""
        filename = os.path.basename(data["filename"])
        try:
            result = cms_initiate_upload(
                user,
                data["name"],
                filename,
                data["file_size"],
                data["agent_type"],
                scope=resource_placement(user, data),
            )
        except CMSError as exc:
            # The CMS and storage layers log rejections and failures where they are raised.
            logger.warning(
                "Upload initiation failed: principal_uuid=%s filename=%s exc_type=%s",
                user.principal.uuid,
                safe_log_value(filename),
                type(exc).__name__,
            )
            return self.bad_request(classify_user_message(str(exc), default="Upload could not be initiated"))

        set_upload_in_progress(_upload_session(request), True, upload_token=str(result["upload_token"]))
        logger.info(
            "Upload initiated: principal_uuid=%s filename=%s size=%d",
            safe_log_value(str(user.principal.uuid)),
            safe_log_value(filename),
            data["file_size"],
        )
        return Response(result)


class UploadCompleteView(MissionControlAPIView):
    """Complete a presigned agent upload."""

    required_actions = scope_actions("manage_content")

    permission_classes = [IsAuthenticatedSessionOrApiToken, HasMissionControlActor]

    @extend_schema(
        request=UploadCompleteSerializer,
        responses=UploadCompleteResponseSerializer,
        operation_id="api_v1_mission_control_upload_complete",
    )
    def post(self, request: Request) -> Response:
        """Finalize a completed upload and clear the session upload marker."""
        data, error = _validated(self, UploadCompleteSerializer, request.data)
        if error is not None:
            return error
        assert data is not None

        user = self.actor_credential()
        try:
            agent = cms_complete_upload(user, data.get("upload_token", ""))
        except CMSError as exc:
            _clear_matching_upload(request, data.get("upload_token", ""))
            logger.warning(
                "Upload completion failed: principal_uuid=%s exc_type=%s", user.principal.uuid, type(exc).__name__
            )
            return self.bad_request(classify_user_message(str(exc), default="Upload could not be completed"))

        _clear_matching_upload(request, data.get("upload_token", ""))
        logger.info("Upload completed: user=%s agent_id=%s", safe_log_value(str(user.principal.uuid)), agent.id)
        return Response(
            {
                "success": True,
                "agent_id": agent.id,
                "message": f"Agent '{agent.name}' uploaded successfully.",
            }
        )


class UploadCancelView(MissionControlAPIView):
    """Cancel an in-progress agent upload."""

    required_actions = scope_actions("manage_content")

    permission_classes = [IsAuthenticatedSessionOrApiToken, HasMissionControlActor]

    @extend_schema(
        request=UploadCancelSerializer,
        responses=SuccessResponseSerializer,
        operation_id="api_v1_mission_control_upload_cancel",
    )
    def post(self, request: Request) -> Response:
        """Cancel a validated current upload and clear the session marker."""
        try:
            raw_data = request.data
        except ParseError:
            return self.bad_request("Invalid request")
        data, error = _validated(self, UploadCancelSerializer, raw_data)
        if error is not None:
            return error
        assert data is not None

        upload_token = data["upload_token"]
        user = self.actor_credential()
        if upload_token:
            try:
                # CMS validates the token and absorbs best-effort storage cleanup failures.
                cms_cancel_upload(user, upload_token)
                logger.info("Cancelled upload cleaned up: user=%s", safe_log_value(str(user.principal.uuid)))
            except (ValueError, CMSError):
                response = self.bad_request("Upload cancel token is invalid or stale")
            else:
                _clear_matching_upload(request, upload_token)
                response = Response({"success": True})
        else:
            response = self.bad_request("Upload cancel token is invalid or stale")

        return response
