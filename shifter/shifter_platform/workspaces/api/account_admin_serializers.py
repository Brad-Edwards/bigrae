"""Closed wire declarations and bounded projections for account administration."""

from __future__ import annotations

from rest_framework import serializers

from shared.api.closed_serializer import ClosedSerializer
from workspaces.models import EGRESS_POLICY_CHOICES, QUOTA_MODE_CHOICES, QUOTA_RESOURCE_CHOICES


class AccountCommandSerializer(ClosedSerializer):
    """Closed account declaration with an explicit individual principal."""

    kind = serializers.ChoiceField(choices=("individual", "team", "enterprise"))
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    individual_principal_uuid = serializers.UUIDField(required=False)
    publication_receipt = serializers.UUIDField(required=False)

    def validate(self, attrs: dict[str, object]) -> dict[str, object]:
        """Require an individual principal only for individual account declarations."""
        if (attrs.get("kind") == "individual") != ("individual_principal_uuid" in attrs):
            raise serializers.ValidationError({"individual_principal_uuid": "Invalid account declaration."})
        return attrs


class AccountPageQuerySerializer(serializers.Serializer):
    """Bounded public collection pagination."""

    offset = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=50, min_value=1, max_value=100)


class AccountSearchQuerySerializer(AccountPageQuerySerializer):
    """Search is applied to the policy-visible account count/page."""

    search = serializers.CharField(required=False, default="", allow_blank=True, max_length=200)


class IntegrationOrganizationQuerySerializer(AccountPageQuerySerializer):
    """Select the integration whose organization grants are being discovered."""

    integration = serializers.ChoiceField(choices=("adapters", "model_sources"))


class WorkspacePageQuerySerializer(AccountSearchQuerySerializer):
    """Archived discovery is explicit, not a widening of ordinary lists."""

    include_archived = serializers.BooleanField(required=False, default=False)


class AccountViewSerializer(serializers.Serializer):
    """Public account fields; internal database IDs never cross the API."""

    uuid = serializers.UUIDField(read_only=True)
    kind = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)


class AccountPageSerializer(serializers.Serializer):
    """Filtered count and one authorized page."""

    count = serializers.IntegerField(read_only=True)
    results = AccountViewSerializer(many=True, read_only=True)


class SubdivisionCommandSerializer(ClosedSerializer):
    """One bounded display name for a real business-account subdivision."""

    name = serializers.CharField(max_length=200, trim_whitespace=True)


class SubdivisionCreationCommandSerializer(SubdivisionCommandSerializer):
    """Retry a committed subdivision through its exact publication receipt."""

    publication_receipt = serializers.UUIDField(required=False)


class HierarchyPendingReceiptSerializer(serializers.Serializer):
    """A committed creation awaiting policy publication."""

    uuid = serializers.UUIDField(read_only=True)
    publication_receipt = serializers.UUIDField(read_only=True)
    publication_state = serializers.ChoiceField(choices=("pending",), read_only=True)


class OrganizationViewSerializer(serializers.Serializer):
    """Public organization identity and default marker."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    is_default = serializers.BooleanField(read_only=True)


class WorkspaceViewSerializer(serializers.Serializer):
    """Public workspace identity and default marker."""

    uuid = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    is_default = serializers.BooleanField(read_only=True)
    archived_at = serializers.DateTimeField(read_only=True, allow_null=True)
    egress_policy = serializers.CharField(read_only=True)
    owner_principal_uuids = serializers.ListField(child=serializers.UUIDField(), read_only=True)


class OrganizationPageSerializer(serializers.Serializer):
    """A bounded page counted after organization policy filtering."""

    count = serializers.IntegerField(read_only=True)
    results = OrganizationViewSerializer(many=True, read_only=True)


class WorkspacePageSerializer(serializers.Serializer):
    """A bounded page counted after workspace policy filtering."""

    count = serializers.IntegerField(read_only=True)
    results = WorkspaceViewSerializer(many=True, read_only=True)


class AccountMemberCommandSerializer(ClosedSerializer):
    """Add one active principal without inferring a group or role."""

    principal_uuid = serializers.UUIDField()
    principal_kind = serializers.ChoiceField(choices=("human", "service"))


class AccountMemberViewSerializer(serializers.Serializer):
    """Public membership identity without contact or policy fields."""

    principal_uuid = serializers.UUIDField(read_only=True)


class AccountMemberPageSerializer(serializers.Serializer):
    """Membership collection counted after the parent policy check."""

    count = serializers.IntegerField(read_only=True)
    results = AccountMemberViewSerializer(many=True, read_only=True)


class WorkspaceQuotaPolicyCommandSerializer(ClosedSerializer):
    """One closed workspace quota update."""

    resource = serializers.ChoiceField(choices=QUOTA_RESOURCE_CHOICES)
    limit = serializers.IntegerField(min_value=0)
    mode = serializers.ChoiceField(choices=QUOTA_MODE_CHOICES)


class WorkspaceEgressPolicyCommandSerializer(ClosedSerializer):
    """One closed workspace egress selection."""

    egress_policy = serializers.ChoiceField(choices=EGRESS_POLICY_CHOICES)


class EmptyAccountCommandSerializer(ClosedSerializer):
    """Closed body for lifecycle commands without client-owned fields."""
