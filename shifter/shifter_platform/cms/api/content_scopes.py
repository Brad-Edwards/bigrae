"""Bounded native content-parent discovery for Scenario Editor."""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from cms.services import content_scope_options
from shared.api.closed_serializer import ClosedSerializer

from .permissions import CMS_READ_PERMISSIONS, cms_actor_credential
from .views import ContentAPIView


class ContentScopePageQuerySerializer(ClosedSerializer):
    offset = serializers.IntegerField(min_value=0, default=0)
    limit = serializers.IntegerField(min_value=1, max_value=100, default=50)


class ContentScopeOptionSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("installation", "account", "organization", "workspace"))
    name = serializers.CharField()
    scope_kind = serializers.ChoiceField(choices=("installation", "account"))
    account = serializers.UUIDField(allow_null=True)
    organization = serializers.UUIDField(allow_null=True)
    workspace = serializers.UUIDField(allow_null=True)


class ContentScopePageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    results = ContentScopeOptionSerializer(many=True)


class ContentScopeOptionsView(ContentAPIView):
    permission_classes = CMS_READ_PERMISSIONS

    @extend_schema(parameters=[ContentScopePageQuerySerializer], responses=ContentScopePageSerializer)
    def get(self, request):
        query = ContentScopePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        page = content_scope_options(cms_actor_credential(request), **query.validated_data)
        rows = [
            {
                "kind": row.kind,
                "name": row.name,
                "scope_kind": row.scope.kind,
                "account": row.scope.account_uuid,
                "organization": row.scope.organization_uuid,
                "workspace": row.scope.workspace_uuid,
            }
            for row in page.results
        ]
        return Response(ContentScopePageSerializer({"count": page.count, "results": rows}).data)
