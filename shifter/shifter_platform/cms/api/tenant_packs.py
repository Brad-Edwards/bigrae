"""Native tenant pack installation and removal; multipart bytes are bounded."""

from uuid import UUID

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpRequest
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from cms.services import PackRemovalConflict, request_tenant_pack_removal
from cms.services._tenant_pack_upload import upload_tenant_pack
from shared.api.errors import api_error_response
from shared.api.schema import ApiErrorSerializer
from shared.audit import get_request_id
from shared.exceptions import ValidationError
from workspaces.services import OrganizationAuthorizationError

from .permissions import CMS_WRITE_PERMISSIONS, cms_actor_credential
from .preparation_adapters import PreparationSerializer, preparation_request_audit

# Retained schema constant for session-only callers outside tenant pack services.
SESSION_ONLY_SCHEMA_AUTH: list[dict[str, list[str]]] = [{"cookieAuth": []}]


class _BoundedUpload(FileUploadHandler):
    """Stop multipart ingestion once the archive byte budget is exhausted."""

    def __init__(self, request: HttpRequest) -> None:
        super().__init__(request)
        self.received = 0
        self.exceeded = False

    def receive_data_chunk(self, raw_data: bytes, start: int) -> bytes:
        self.received += len(raw_data)
        if self.received > settings.RAES_PACKAGE_MAX_ARCHIVE_BYTES:
            self.exceeded = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size: int) -> None:
        return None


class TenantPackUploadSerializer(PreparationSerializer):
    """Named pack archive with an optional expected revision digest."""

    name = serializers.RegexField(r"^[A-Za-z0-9_-]{1,100}$")
    archive = serializers.FileField()
    expected_digest = serializers.RegexField(r"^(sha256:[a-f0-9]{64})?$", required=False, default="", allow_blank=True)


class TenantPackInstalledSerializer(serializers.Serializer):
    """The immutable registered pack identity and conformance result."""

    scenario_id = serializers.CharField()
    name = serializers.CharField()
    source_kind = serializers.CharField()
    contract_kind = serializers.CharField()
    contract_profile = serializers.CharField()
    package_version = serializers.CharField()
    package_digest = serializers.CharField()
    conformance_status = serializers.CharField()
    created = serializers.BooleanField()


class TenantPackUploadView(APIView):
    """Validate and install an organization-owned scenario pack."""

    permission_classes = CMS_WRITE_PERMISSIONS
    parser_classes = [MultiPartParser]

    def initialize_request(self, request: HttpRequest, *args: object, **kwargs: object) -> Request:
        # Must precede session/CSRF authentication, which may parse request.POST.
        request.upload_handlers.insert(0, _BoundedUpload(request))
        return super().initialize_request(request, *args, **kwargs)

    @extend_schema(
        request=TenantPackUploadSerializer,
        responses={201: TenantPackInstalledSerializer},
    )
    def post(self, request: Request, organization_uuid: UUID) -> Response:
        try:
            actor = cms_actor_credential(request)
            data = request.data
            if any(getattr(handler, "exceeded", False) for handler in request._request.upload_handlers):
                raise ValidationError("The pack archive exceeds its size limit")
            if any(len(data.getlist(key)) != 1 for key in data):
                raise ValidationError("Submit each pack field exactly once")
            serializer = TenantPackUploadSerializer(data=data)
            serializer.is_valid(raise_exception=True)
            result = upload_tenant_pack(
                user=actor,
                organization_uuid=organization_uuid,
                request_id=get_request_id(request._request),
                **serializer.validated_data,
            )
            return Response(result, status=201)
        except (OrganizationAuthorizationError, ValidationError) as exc:
            denied = isinstance(exc, OrganizationAuthorizationError)
            return api_error_response(
                code="pack_access_denied" if denied else "pack_invalid",
                message=exc.message if isinstance(exc, ValidationError) else "Organization access denied",
                status_code=403 if denied else 400,
                request=request,
            )


class TenantPackRemovalRequestSerializer(PreparationSerializer):
    """Removal pinned to the revision the administrator reviewed."""

    pack_digest = serializers.RegexField(r"^sha256:[a-f0-9]{64}$")


class TenantPackRemovalSerializer(serializers.Serializer):
    """Removal progress; ``pending`` means archive cleanup is still retrying."""

    removal_id = serializers.UUIDField()
    pack_id = serializers.CharField()
    state = serializers.ChoiceField(choices=("pending", "completed"))
    remaining_archives = serializers.IntegerField()


class TenantPackRemovalView(APIView):
    """Remove an organization-owned pack, or resume its pending cleanup."""

    permission_classes = CMS_WRITE_PERMISSIONS

    @extend_schema(
        request=TenantPackRemovalRequestSerializer,
        responses={
            200: OpenApiResponse(TenantPackRemovalSerializer, description="Removed; every archive is deleted."),
            202: OpenApiResponse(TenantPackRemovalSerializer, description="Removed; archive cleanup is retrying."),
            400: OpenApiResponse(ApiErrorSerializer, description="The pack is unavailable in this organization."),
            403: OpenApiResponse(ApiErrorSerializer, description="Organization access denied."),
            409: OpenApiResponse(
                ApiErrorSerializer,
                description=(
                    "pack_in_use (details.blockers, details.blocker_count), pack_changed, pack_busy, "
                    "pack_references_unavailable or pack_storage_unavailable."
                ),
            ),
        },
    )
    def post(self, request: Request, organization_uuid: UUID, pack_id: str) -> Response:
        body = TenantPackRemovalRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = request_tenant_pack_removal(
                cms_actor_credential(request),
                organization_uuid,
                pack_id,
                body.validated_data["pack_digest"],
                audit=preparation_request_audit(request),
            )
        except (OrganizationAuthorizationError, ValidationError, PackRemovalConflict) as exc:
            return _removal_error(request, exc)
        payload = TenantPackRemovalSerializer(
            {
                "removal_id": result.removal_id,
                "pack_id": result.pack_id,
                "state": result.state,
                "remaining_archives": result.remaining_archives,
            }
        ).data
        return Response(payload, status=200 if result.state == "completed" else 202)


def _removal_error(request: Request, exc: Exception) -> Response:
    """Map a refused removal onto the shared error envelope."""
    if isinstance(exc, OrganizationAuthorizationError):
        code, message, status_code, details = "pack_access_denied", "Organization access denied", 403, None
    elif isinstance(exc, PackRemovalConflict):
        code, message, status_code, details = exc.code, exc.message, 409, None
        if exc.code == "pack_in_use":
            details = {
                "blockers": [{"kind": row.kind, "status": row.status, "label": row.label} for row in exc.blockers],
                "blocker_count": exc.total,
            }
    else:
        code, message, status_code, details = (
            "pack_invalid",
            getattr(exc, "message", "The pack is unavailable"),
            400,
            None,
        )
    return api_error_response(code=code, message=message, status_code=status_code, details=details, request=request)
