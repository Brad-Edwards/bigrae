"""Closed query inputs and read-only projections for the communications console (#2052).

Explicit serializers (never ``ModelSerializer``) so the published contract cannot
express a delivery coordinate, an email, a provider detail, or another event's
roster. Split from ``communication.py`` for the python:S104 file-size budget.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from ctf.enums_communication import DeliveryStatus
from shared.api.closed_serializer import ClosedSerializer

_MAX_PAGE = 100
_MAX_OFFSET = 100000


class CommunicationTargetQuerySerializer(ClosedSerializer):
    """Optional workspace filter and a finite page over authorized events."""

    workspace_id = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=_MAX_PAGE, default=_MAX_PAGE)
    offset = serializers.IntegerField(min_value=0, max_value=_MAX_OFFSET, default=0)


class CommunicationTargetOptionsQuerySerializer(ClosedSerializer):
    """An optional exact campaign parent and a finite authorized event page."""

    account = serializers.UUIDField(required=False)
    organization = serializers.UUIDField(required=False)
    workspace = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=_MAX_PAGE, default=_MAX_PAGE)
    offset = serializers.IntegerField(min_value=0, max_value=_MAX_OFFSET, default=0)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if sum(name in attrs for name in ("account", "organization", "workspace")) > 1:
            raise serializers.ValidationError("Invalid value.")
        return attrs


class CommunicationPreviewQuerySerializer(ClosedSerializer):
    """The reviewed revision is always explicit."""

    revision_id = serializers.UUIDField()


class CommunicationPageQuerySerializer(ClosedSerializer):
    """Finite page over a campaign's occurrences."""

    limit = serializers.IntegerField(min_value=1, max_value=_MAX_PAGE, default=25)
    offset = serializers.IntegerField(min_value=0, max_value=_MAX_OFFSET, default=0)


class CommunicationRecipientQuerySerializer(CommunicationPageQuerySerializer):
    """One occurrence and one target event per page."""

    intent_id = serializers.UUIDField()
    event_id = serializers.UUIDField()


class CommunicationWorkspaceSerializer(serializers.Serializer):
    """Public workspace identity and display name."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)


class CommunicationTargetEventSerializer(serializers.Serializer):
    """An event the actor may message, with its display time zone."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    event_timezone = serializers.CharField(read_only=True)
    event_start = serializers.DateTimeField(read_only=True)
    event_end = serializers.DateTimeField(read_only=True)
    workspace_id = serializers.UUIDField(read_only=True)


class CommunicationTargetsSerializer(serializers.Serializer):
    """Authorized workspaces and a bounded page of events."""

    workspaces = CommunicationWorkspaceSerializer(many=True, read_only=True)
    events = CommunicationTargetEventSerializer(many=True, read_only=True)
    next_offset = serializers.IntegerField(read_only=True, allow_null=True)


class CommunicationParentSerializer(serializers.Serializer):
    """The exact direct placement of an authorized event."""

    kind = serializers.ChoiceField(choices=["account", "organization", "workspace"], read_only=True)
    id = serializers.UUIDField(read_only=True)


class CommunicationParentOptionSerializer(CommunicationParentSerializer):
    """One authorized event's parent display label, without directory authority."""

    name = serializers.CharField(read_only=True)


class CommunicationNativeTargetEventSerializer(serializers.Serializer):
    """A native target with explicit placement and display time zone."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    event_timezone = serializers.CharField(read_only=True)
    event_start = serializers.DateTimeField(read_only=True)
    event_end = serializers.DateTimeField(read_only=True)
    parent = CommunicationParentSerializer(read_only=True)


class CommunicationTargetOptionsSerializer(serializers.Serializer):
    """All currently authorized parents and a bounded page of their events."""

    parents = CommunicationParentOptionSerializer(many=True, read_only=True)
    events = CommunicationNativeTargetEventSerializer(many=True, read_only=True)
    next_offset = serializers.IntegerField(read_only=True, allow_null=True)


class CommunicationTriggerProjectionSerializer(serializers.Serializer):
    """The closed trigger kind and its UTC instant (null when untimed)."""

    kind = serializers.CharField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True, allow_null=True)


class CommunicationPreviewRevisionSerializer(serializers.Serializer):
    """The immutable revision under review."""

    id = serializers.UUIDField(read_only=True)
    revision_number = serializers.IntegerField(read_only=True)
    subject = serializers.CharField(read_only=True)
    body = serializers.CharField(read_only=True)
    content_profile = serializers.CharField(read_only=True)
    content_digest = serializers.CharField(read_only=True)


class CommunicationChannelAvailabilitySerializer(serializers.Serializer):
    """Whether a selected channel has a registered transport now."""

    channel = serializers.CharField(read_only=True)
    available = serializers.BooleanField(read_only=True)


class CommunicationPreviewEventSerializer(serializers.Serializer):
    """Current audience estimate and local due time for one target event."""

    event_id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    event_timezone = serializers.CharField(read_only=True)
    recipient_count = serializers.IntegerField(read_only=True)
    # ISO 8601 with the event's own UTC offset; a DateTimeField would normalize the zone away.
    local_due_at = serializers.CharField(read_only=True, allow_null=True)


class CommunicationPreviewSerializer(serializers.Serializer):
    """A side-effect-free preview; counts are estimates, not a delivery claim."""

    campaign_id = serializers.UUIDField(read_only=True)
    revision = CommunicationPreviewRevisionSerializer(read_only=True)
    channels = CommunicationChannelAvailabilitySerializer(many=True, read_only=True)
    acknowledgement_policy = serializers.CharField(read_only=True)
    trigger = CommunicationTriggerProjectionSerializer(read_only=True)
    events = CommunicationPreviewEventSerializer(many=True, read_only=True)
    recipient_total = serializers.IntegerField(read_only=True)
    audience_cap = serializers.IntegerField(read_only=True)
    exceeds_audience_cap = serializers.BooleanField(read_only=True)
    observed_at = serializers.DateTimeField(read_only=True)


class CommunicationChannelCountsSerializer(serializers.Serializer):
    """Delivery-command counts by truthful status for one channel."""

    channel = serializers.CharField(read_only=True)

    def get_fields(self) -> dict[str, serializers.Field]:
        """Declare one count per closed delivery status."""
        fields = super().get_fields()
        for status in DeliveryStatus:
            fields[status.value] = serializers.IntegerField(read_only=True)
        return fields


class CommunicationDeliveryEventSerializer(serializers.Serializer):
    """Per-event recipients, inbox receipts, and per-channel command counts."""

    event_id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    recipients = serializers.IntegerField(read_only=True)
    inbox_available = serializers.IntegerField(read_only=True)
    read = serializers.IntegerField(read_only=True)
    acknowledged = serializers.IntegerField(read_only=True)
    channels = CommunicationChannelCountsSerializer(many=True, read_only=True)


class CommunicationDeliveryTotalsSerializer(serializers.Serializer):
    """Occurrence totals across all target events."""

    recipients = serializers.IntegerField(read_only=True)
    inbox_available = serializers.IntegerField(read_only=True)
    read = serializers.IntegerField(read_only=True)
    acknowledged = serializers.IntegerField(read_only=True)
    channels = CommunicationChannelCountsSerializer(many=True, read_only=True)


class CommunicationDeliveryOccurrenceSerializer(serializers.Serializer):
    """One occurrence: admission state, release-task state, and delivery facts."""

    intent_id = serializers.UUIDField(read_only=True)
    status = serializers.CharField(read_only=True)
    trigger_kind = serializers.CharField(read_only=True)
    revision_number = serializers.IntegerField(read_only=True)
    due_at = serializers.DateTimeField(read_only=True, allow_null=True)
    released_at = serializers.DateTimeField(read_only=True, allow_null=True)
    schedule_status = serializers.CharField(read_only=True, allow_null=True)
    events = CommunicationDeliveryEventSerializer(many=True, read_only=True)
    totals = CommunicationDeliveryTotalsSerializer(read_only=True)


class CommunicationDeliveryReportSerializer(serializers.Serializer):
    """A bounded page of occurrences, newest first."""

    results = CommunicationDeliveryOccurrenceSerializer(many=True, read_only=True)
    next_offset = serializers.IntegerField(read_only=True, allow_null=True)


class CommunicationRecipientChannelSerializer(serializers.Serializer):
    """One delivery command's status for a recipient."""

    channel = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)


class CommunicationRecipientSerializer(serializers.Serializer):
    """Minimal recipient state: name and team only, never a coordinate."""

    participant_id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True, allow_null=True)
    team = serializers.CharField(read_only=True, allow_null=True)
    removed = serializers.BooleanField(read_only=True)
    inbox_available = serializers.BooleanField(read_only=True)
    read_at = serializers.DateTimeField(read_only=True, allow_null=True)
    acknowledged_at = serializers.DateTimeField(read_only=True, allow_null=True)
    channels = CommunicationRecipientChannelSerializer(many=True, read_only=True)


class CommunicationRecipientPageSerializer(serializers.Serializer):
    """A bounded page of recipient rows."""

    results = CommunicationRecipientSerializer(many=True, read_only=True)
    next_offset = serializers.IntegerField(read_only=True, allow_null=True)


class CommunicationActorSerializer(serializers.Serializer):
    """Display actor for a timeline entry."""

    label = serializers.CharField(read_only=True)
    via = serializers.CharField(read_only=True, help_text="session | api_token | credential | scheduler")
    principal_uuid = serializers.UUIDField(read_only=True, allow_null=True)


class CommunicationActivityEntrySerializer(serializers.Serializer):
    """One body-free timeline entry."""

    kind = serializers.ChoiceField(
        choices=["created", "revised", "scheduled", "released", "expired", "fenced", "cancelled"], read_only=True
    )
    at = serializers.DateTimeField(read_only=True)
    actor = CommunicationActorSerializer(read_only=True, allow_null=True)
    revision_number = serializers.IntegerField(read_only=True, allow_null=True)
    intent_id = serializers.UUIDField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True, allow_null=True)
    due_at = serializers.DateTimeField(read_only=True, allow_null=True)


class CommunicationActivitySerializer(serializers.Serializer):
    """The campaign timeline; ``truncated`` reports the row bound."""

    entries = CommunicationActivityEntrySerializer(many=True, read_only=True)
    truncated = serializers.BooleanField(read_only=True)
