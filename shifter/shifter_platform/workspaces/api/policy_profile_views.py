"""Production account/profile adapters backed by policy-aware services."""

from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response

from shared.api.principals import authenticated_credential
from shared.audit import request_audit
from shared.authorization import configured_authorization_provider
from workspaces import services
from workspaces.api.account_admin_views import (
    AccountDetailView,
    AccountPageQuerySerializer,
    AccountPageSerializer,
    AccountViewSerializer,
    SubdivisionCommandSerializer,
    _AccountAPIView,
)
from workspaces.api.serializers import (
    OrganizationProfileSerializer,
    OrganizationProfileUpdateSerializer,
    WorkspaceQuotaSerializer,
)


class AccountPrincipalContextSerializer(serializers.Serializer):
    """Current display scope and policy hints, never authorization credentials."""

    principal_uuid = serializers.UUIDField(read_only=True)
    principal_kind = serializers.CharField(read_only=True)
    accounts = AccountPageSerializer(read_only=True)
    individual_account = AccountViewSerializer(read_only=True, allow_null=True)
    installation_actions = serializers.ListField(child=serializers.CharField(), read_only=True)
    has_descendant_scopes = serializers.BooleanField(read_only=True)


class AccountPrincipalContextView(_AccountAPIView):
    """Prepared replacement for workspace-only or staff-only presentation assumptions."""

    @extend_schema(parameters=[AccountPageQuerySerializer], responses=AccountPrincipalContextSerializer)
    def get(self, request: Request) -> Response:
        query = AccountPageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        context = services.admin_principal_context(
            authenticated_credential(request), configured_authorization_provider(), **query.validated_data
        )
        return Response(AccountPrincipalContextSerializer(context).data)


class PolicyAccountDetailView(AccountDetailView):
    """Read/update the canonical account name without altering account identity."""

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            }
        },
        responses=AccountViewSerializer,
    )
    def patch(self, request: Request, account_uuid: UUID) -> Response:
        command = SubdivisionCommandSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        result = services.admin_update_account(
            actor,
            configured_authorization_provider(),
            account_uuid,
            command.validated_data,
            audit=request_audit(request, credential=actor),
        )
        return Response(AccountViewSerializer(result).data)


class PolicyOrganizationProfileView(_AccountAPIView):
    """Retain the existing profile response and PATCH mask under exact account policy."""

    @extend_schema(responses=OrganizationProfileSerializer)
    def get(self, request: Request, account_uuid: UUID, organization_uuid: UUID) -> Response:
        result = services.admin_get_organization_profile(
            authenticated_credential(request), configured_authorization_provider(), account_uuid, organization_uuid
        )
        return Response(OrganizationProfileSerializer(result).data)

    @extend_schema(request=OrganizationProfileUpdateSerializer, responses=OrganizationProfileSerializer)
    def patch(self, request: Request, account_uuid: UUID, organization_uuid: UUID) -> Response:
        command = OrganizationProfileUpdateSerializer(data=request.data)
        command.is_valid(raise_exception=True)
        actor = authenticated_credential(request)
        result = services.admin_update_organization_profile(
            actor,
            configured_authorization_provider(),
            account_uuid,
            organization_uuid,
            command.validated_data,
            audit=request_audit(request, credential=actor),
        )
        return Response(OrganizationProfileSerializer(result).data)


class PolicyWorkspaceQuotaView(_AccountAPIView):
    """Expose the same bounded quota projection to authorized humans/services."""

    @extend_schema(responses=WorkspaceQuotaSerializer, operation_id="api_v1_workspace_quota")
    def get(self, request: Request, workspace_uuid: UUID) -> Response:
        result = services.admin_workspace_quota_usage(
            authenticated_credential(request), configured_authorization_provider(), workspace_uuid
        )
        return Response(WorkspaceQuotaSerializer(result).data)


class AdministrationScopeQuerySerializer(AccountPageQuerySerializer):
    """Select one bounded descendant collection."""

    kind = serializers.ChoiceField(choices=("organization", "workspace"))


class AdministrationScopeSerializer(serializers.Serializer):
    """An independently authorized scope and non-secret routing ancestry."""

    kind = serializers.ChoiceField(choices=("organization", "workspace"), read_only=True)
    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    account_uuid = serializers.UUIDField(read_only=True)
    organization_uuid = serializers.UUIDField(read_only=True)
    is_default = serializers.BooleanField(read_only=True)


class AdministrationScopePageSerializer(serializers.Serializer):
    count = serializers.IntegerField(read_only=True)
    results = AdministrationScopeSerializer(many=True, read_only=True)


class AdministrationScopeCollectionView(_AccountAPIView):
    """Discover descendant administration without a parent account selector."""

    @extend_schema(parameters=[AdministrationScopeQuerySerializer], responses=AdministrationScopePageSerializer)
    def get(self, request: Request) -> Response:
        query = AdministrationScopeQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = services.admin_list_descendant_scopes(
            authenticated_credential(request), configured_authorization_provider(), **query.validated_data
        )
        return Response(AdministrationScopePageSerializer(page).data)
