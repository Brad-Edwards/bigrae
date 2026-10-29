"""Canonical RAES catalog and package-ingestion API views."""

from __future__ import annotations

import logging
from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from cms.api.content_scope import ContentScopeSerializer, content_request_scope
from cms.api.permissions import CMS_READ_PERMISSIONS, CMS_WRITE_PERMISSIONS, cms_actor_credential
from cms.api.serializers import (
    CatalogEntrySerializer,
    PackRegistrationResultSerializer,
    PackRegistrationSerializer,
    ScenarioDetailSerializer,
    ScenarioMetadataStateSerializer,
    ScenarioMetadataUpdateSerializer,
    ScenarioRealizabilitySerializer,
)
from cms.exceptions import CMSError
from cms.scenario_editor._common import ScenarioEditorError
from cms.scenario_editor._metadata import update_metadata
from cms.scenarios import catalog_presentation
from cms.scenarios import realizability as scenario_realizability
from cms.services import PackRegistrationRequest, register_pack
from cms.services._content_authority import ContentAuthorityError
from shared.api.errors import api_error_response
from shared.audit import get_request_id
from shared.credentials import CredentialContext
from shared.errors import classify_user_message
from shared.log_sanitize import safe_log_value

logger = logging.getLogger(__name__)


def _actor_credential(request: Request) -> CredentialContext:
    """Preserve the authenticated proof independently of historical human attribution."""
    return cms_actor_credential(request)


def _not_found(request: Request, message: str = "Scenario not found") -> Response:
    """Return the shared bounded 404 response for a missing catalog source."""
    return api_error_response(code="not_found", message=message, status_code=status.HTTP_404_NOT_FOUND, request=request)


def _raes_detail_payload(scenario_id: str, *, user: CredentialContext) -> dict[str, Any] | None:
    """Build the read-only Scenario Catalog projection for one RAES source."""
    entry = catalog_presentation.get_catalog_presentation(scenario_id, user=user)
    if entry is None:
        return None
    return {
        "id": entry["id"],
        "name": entry["name"],
        "scenario_type": "raes",
        "source": "raes",
        "enabled": bool(entry.get("enabled", True)),
        "staff_only": bool(entry.get("staff_only", False)),
        "launchable": bool(entry.get("launchable", False)),
        "raes": entry.get("raes"),
    }


class ContentAPIView(APIView):
    """Keep denied content authority behind the bounded public error envelope."""

    def handle_exception(self, exc: Exception) -> Response:
        """Keep missing or denied content opaque while preserving other DRF errors."""
        if isinstance(exc, CMSError):
            if self.kwargs.get("scenario_id"):
                return _not_found(self.request)
            return api_error_response(
                code="forbidden", message="Content unavailable", status_code=403, request=self.request
            )
        return super().handle_exception(exc)


class CatalogListView(ContentAPIView):
    """List the canonical RAES-backed catalog projection."""

    permission_classes = CMS_READ_PERMISSIONS

    @extend_schema(parameters=[ContentScopeSerializer], responses=CatalogEntrySerializer(many=True))
    def get(self, request: Request) -> Response:
        """List the catalog visible to the current credential in its explicit content scope."""
        query = ContentScopeSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(
            CatalogEntrySerializer(
                catalog_presentation.list_catalog_presentations(
                    user=_actor_credential(request),
                    scope=content_request_scope(query.validated_data),
                    include_unavailable=True,
                ),
                many=True,
            ).data
        )


class CatalogDetailView(ContentAPIView):
    """Return one canonical RAES-backed catalog projection."""

    permission_classes = CMS_READ_PERMISSIONS

    @extend_schema(responses=CatalogEntrySerializer)
    def get(self, request: Request, scenario_id: str) -> Response:
        """Read one visible catalog entry or return the opaque not-found response."""
        entry = catalog_presentation.get_catalog_presentation(scenario_id, user=_actor_credential(request))
        if entry is None:
            return _not_found(request, "Catalog entry not found")
        return Response(CatalogEntrySerializer(entry).data)


class PackRegisterView(ContentAPIView):
    """Register an untrusted pack through the uniform ingestion service."""

    permission_classes = CMS_WRITE_PERMISSIONS

    @extend_schema(request=PackRegistrationSerializer, responses={201: PackRegistrationResultSerializer})
    def post(self, request: Request) -> Response:
        """Register a validated pack declaration under explicit native content authority."""
        serializer = PackRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = register_pack(
                user=_actor_credential(request),
                scope=content_request_scope(data),
                request=PackRegistrationRequest(
                    scenario_id=data["scenario_id"],
                    source_kind=data["source_kind"],
                    contract_kind=data["contract_kind"],
                    contract_profile=data["contract_profile"],
                    package_ref=data["package_ref"],
                    package_version=data["package_version"],
                    package_digest=data["package_digest"],
                    expected_package_digest=data["expected_package_digest"],
                    lock_ref=data["lock_ref"],
                    lock_digest=data["lock_digest"],
                    provenance=data["provenance"],
                ),
                request_id=get_request_id(request._request),
            )
        except ContentAuthorityError:
            return api_error_response(code="forbidden", message="Content unavailable", status_code=403, request=request)
        except CMSError as exc:
            # A refused registration is a client outcome (400); the audit seam logs its own failures.
            logger.warning(
                "pack registration rejected scenario_id=%s reason=%s",
                safe_log_value(data["scenario_id"]),
                safe_log_value(exc.message),
            )
            return api_error_response(
                code="invalid",
                message=classify_user_message(
                    str(exc), default="The pack registration request could not be completed."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return Response(
            {
                "scenario_id": result.scenario_id,
                "source_kind": result.source_kind,
                "conformance_status": result.conformance_status,
            },
            status=status.HTTP_201_CREATED,
        )


class ScenarioResourceView(ContentAPIView):
    """Return a read-only RAES scenario detail projection."""

    permission_classes = CMS_READ_PERMISSIONS

    @extend_schema(responses=ScenarioDetailSerializer)
    def get(self, request: Request, scenario_id: str) -> Response:
        """Return the authorized RAES-backed scenario projection."""
        payload = _raes_detail_payload(scenario_id, user=_actor_credential(request))
        if payload is None:
            return _not_found(request)
        return Response(ScenarioDetailSerializer(payload).data)


class ScenarioRealizabilityView(ContentAPIView):
    """Return the backend realizability assessment for one RAES source."""

    permission_classes = CMS_READ_PERMISSIONS

    @extend_schema(responses=ScenarioRealizabilitySerializer)
    def get(self, request: Request, scenario_id: str) -> Response:
        """Read realizability only after current authority admits the source scope."""
        from cms.models import RaesPackageSource
        from cms.services._content_authority import content_scope, require_content_scope

        source = RaesPackageSource.objects.filter(scenario_id=scenario_id).first()
        if source is None:
            return _not_found(request)
        actor = _actor_credential(request)
        scope = content_scope(source)
        require_content_scope(actor, scope, "read_content")
        result = scenario_realizability.get_scenario_realizability(scenario_id, credential=actor, scope=scope)
        if result is None:
            return _not_found(request)
        return Response(ScenarioRealizabilitySerializer(result).data)


class ScenarioMetadataView(ContentAPIView):
    """Update the availability/audience overlay for a RAES package source."""

    permission_classes = CMS_WRITE_PERMISSIONS

    @extend_schema(request=ScenarioMetadataUpdateSerializer, responses=ScenarioMetadataStateSerializer)
    def patch(self, request: Request, scenario_id: str) -> Response:
        """Update validated catalog metadata under the authenticated native proof."""
        serializer = ScenarioMetadataUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            metadata = update_metadata(
                _actor_credential(request),
                scenario_id,
                enabled=serializer.validated_data.get("enabled"),
                staff_only=serializer.validated_data.get("staff_only"),
            )
        except ScenarioEditorError as exc:
            return api_error_response(
                code="invalid",
                message=exc.public_message,
                status_code=status.HTTP_400_BAD_REQUEST,
                request=request,
            )
        return Response(
            {
                "scenario_id": scenario_id,
                "enabled": metadata.enabled,
                "staff_only": metadata.staff_only,
            }
        )
