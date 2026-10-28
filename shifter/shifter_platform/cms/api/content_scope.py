"""Closed explicit placement for CMS content collections and ingestion."""

from typing import Any

from rest_framework import serializers

from shared.api.closed_serializer import ClosedSerializer
from shared.identity_scope import ResourceScope
from workspaces.services import resolve_resource_scope


class ContentScopeSerializer(ClosedSerializer):
    """Global catalog requests select installation; private requests name exact ancestry."""

    scope_kind = serializers.ChoiceField(choices=("installation", "account"), default="installation")
    account = serializers.UUIDField(required=False)
    organization = serializers.UUIDField(required=False)
    workspace = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            scope = ResourceScope(
                attrs["scope_kind"], attrs.get("account"), attrs.get("organization"), attrs.get("workspace")
            )
            resolve_resource_scope(scope)
        except Exception:
            raise serializers.ValidationError("Content scope unavailable") from None
        return attrs


def content_request_scope(values: dict[str, Any]) -> ResourceScope:
    """Consume only validated fields; the owning service still authorizes the scope."""
    return ResourceScope(
        values["scope_kind"], values.get("account"), values.get("organization"), values.get("workspace")
    )
