"""Session-only, tenant-scoped model choices for range and event forms."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from cms.api.model_sources import ModelSourceViewSerializer
from cms.api.preparation_adapters import PreparationSerializer
from cms.services import model_source_alias_options
from shared.api.errors import api_error_response
from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.api_tokens.permissions import require_scope
from shared.api_tokens.scopes import AUTHORIZATION_ACTION_SCOPES
from shared.model_access import ContractError
from workspaces.services import AccountScopeError, OrganizationAuthorizationError, workspace_option


class SourceOptionsQuerySerializer(PreparationSerializer):
    """Workspace, scenario, and purpose filters for available source choices."""

    scenario = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    workspace = serializers.UUIDField(required=False)
    purpose = serializers.ChoiceField(choices=["range", "ctf", "admin"], default="range")


class SourceWorkspaceSerializer(serializers.Serializer):
    """Workspace identity displayed alongside tenant source options."""

    uuid = serializers.UUIDField()
    name = serializers.CharField()
    organization_name = serializers.CharField()
    is_personal = serializers.BooleanField()


class AliasSourceOptionsSerializer(serializers.Serializer):
    """Compatible sources and mixing policy for one logical alias."""

    logical_alias = serializers.CharField()
    multiple_allowed = serializers.BooleanField()
    sources = ModelSourceViewSerializer(many=True)


class SourceOptionsSerializer(serializers.Serializer):
    """Authorized workspaces and scenario-specific source choices."""

    workspaces = SourceWorkspaceSerializer(many=True)
    workspace = serializers.UUIDField(allow_null=True)
    aliases = AliasSourceOptionsSerializer(many=True)
    available = serializers.BooleanField()


class ModelSourceOptionsView(APIView):
    """Discover source choices under the exact organization policy action."""

    permission_classes = [
        IsAuthenticatedSessionOrApiToken,
        require_scope(AUTHORIZATION_ACTION_SCOPES["organization.manage_model_sources"]),
    ]

    @extend_schema(parameters=[SourceOptionsQuerySerializer], responses=SourceOptionsSerializer)
    def get(self, request: Request) -> Response:
        query = SourceOptionsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        values = query.validated_data
        selected = values.get("workspace")
        aliases: list[dict[str, object]] = []
        available = False
        workspaces: list[dict[str, object]] = []
        try:
            if selected:
                workspace = workspace_option(selected)
                aliases, available = model_source_alias_options(
                    authenticated_credential(request), workspace.organization_uuid, values["scenario"]
                )
                workspaces.append(
                    {
                        "uuid": workspace.uuid,
                        "name": workspace.name,
                        "organization_name": workspace.organization_name,
                        "is_personal": False,
                    }
                )
        except (AccountScopeError, OrganizationAuthorizationError, ContractError, ValueError):
            return api_error_response(
                code="source_options_unavailable",
                message="Model sources are unavailable for this workspace and scenario.",
                status_code=403,
                request=request,
            )
        return Response(
            {
                "workspaces": workspaces,
                "workspace": selected,
                "aliases": aliases,
                "available": available,
            }
        )
