"""Explicit native placement for Mission Control creation commands."""

from rest_framework import serializers

from cms.services import mission_control_resource_scope
from shared.api.closed_serializer import ClosedSerializer
from shared.identity_scope import ResourceScope


class ResourcePlacementSerializer(ClosedSerializer):
    """Optional exact account ancestry; unqualified humans use their personal account."""

    account_uuid = serializers.UUIDField(required=False)
    organization_uuid = serializers.UUIDField(required=False)
    workspace_uuid = serializers.UUIDField(required=False)


def resource_placement(credential, values) -> ResourceScope:
    """Resolve SQL ancestry; the owning service separately evaluates its action."""
    return mission_control_resource_scope(
        credential.principal,
        account_uuid=values.get("account_uuid"),
        organization_uuid=values.get("organization_uuid"),
        workspace_uuid=values.get("workspace_uuid"),
    )
