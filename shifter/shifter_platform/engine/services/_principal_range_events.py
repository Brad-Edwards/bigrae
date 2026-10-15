"""Verify a principal status notification against accepted native state and outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.models import Range

from collections.abc import Mapping
from uuid import UUID

from shared.messages.events import EVENT_TYPE_STATUS_UPDATED
from shared.operation_envelope import canonical_payload_digest

from ._launch_authority import range_runtime_binding_digest
from ._range_authority import RangeAuthorityProjection, project_range_authority

_RANGE_CALLBACK_UNAVAILABLE = "Range callback unavailable"


def range_status_authority_fields(row: Range) -> dict[str, object]:
    """Pin the status mirror to native generation, current scope and owner."""
    if row.owner_principal_uuid is None:
        return {}
    if row.resource_generation is None:
        raise ValueError("Range generation unavailable")
    return {
        "owner_principal_uuid": str(row.owner_principal_uuid),
        "resource_generation": str(row.resource_generation),
        "operation_id": str(row.provisioner_operation_id) if row.provisioner_operation_id else None,
        "authority_binding_digest": range_runtime_binding_digest(row),
    }


def verified_range_status_projection(event: Mapping[str, object]) -> RangeAuthorityProjection | None:
    """A callback mirrors accepted facts; it cannot authorize a runtime mutation."""
    from engine.models import Range

    try:
        range_id = _verified_callback_range_id(event)
        row = Range.objects.select_related("request").get(pk=range_id)
        if row.owner_principal_uuid is None or row.request is None or row.status != event["new_status"]:
            raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
        fields = range_status_authority_fields(row)
        if fields.keys() - event.keys() or any(event[key] != value for key, value in fields.items()):
            raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
        if event["request_id"] != str(row.request.request_id):
            raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
        return project_range_authority(row.request.request_id)
    except Exception:
        return None


def _verified_callback_range_id(event: Mapping[str, object]) -> int:
    """Accept only the canonical callback bytes retained in the native outbox."""
    from engine.models import RangeEventOutbox

    if event.get("event_type") != EVENT_TYPE_STATUS_UPDATED or type(event.get("range_id")) is not int:
        raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
    identifier = UUID(str(event["event_id"]))
    if str(identifier) != event["event_id"]:
        raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
    stored = RangeEventOutbox.objects.filter(event_id=identifier, event_type=EVENT_TYPE_STATUS_UPDATED).first()
    if stored is None or canonical_payload_digest(stored.payload) != canonical_payload_digest(dict(event)):
        raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
    range_id = event["range_id"]
    if not isinstance(range_id, int) or isinstance(range_id, bool):
        raise ValueError(_RANGE_CALLBACK_UNAVAILABLE)
    return range_id
