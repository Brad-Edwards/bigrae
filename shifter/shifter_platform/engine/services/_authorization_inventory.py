"""SQL-authoritative range inventory for authorization delegation proofs."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from shared.authorization import TargetRef, bind_authorization_descendant_resolver
from shared.authorization.inventory import (
    AuthorizationDescendantResolutionError,
    bind_authorization_event_range_resolver,
    bind_authorization_range_binding_resolver,
)
from shared.identity_scope import ResourceScope


def list_authorization_range_uuids(workspace_ids: Sequence[int], limit: int) -> tuple[UUID, ...]:
    """Return at most ``limit`` range UUIDs for the exact workspace ids."""
    from engine.models import Range

    if limit <= 0 or not workspace_ids:
        return ()
    values = tuple(
        Range.objects.filter(workspace_id__in=workspace_ids)
        .order_by("uuid")
        .values_list("cms_range_uuid", flat=True)[:limit]
    )
    identities = []
    for value in values:
        if value is None:
            raise AuthorizationDescendantResolutionError("Range public identity unavailable")
        identities.append(value)
    return tuple(identities)


def _authorization_event_range_targets(event_uuid: UUID, limit: int) -> tuple[TargetRef, ...]:
    """Include native event children with their independently verified ancestry."""
    from engine.models import Range

    from ._range_authority import project_range_authority

    result = []
    for row in Range.objects.filter(event_uuid=event_uuid).select_related("request").order_by("uuid")[:limit]:
        if row.request is None:
            raise AuthorizationDescendantResolutionError("Range request unavailable")
        projection = project_range_authority(row.request.request_id)
        if projection.parent != TargetRef("event", event_uuid):
            raise AuthorizationDescendantResolutionError("Range event ancestry changed")
        result.append(TargetRef("range", projection.public_uuid))
    return tuple(result)


def _authorization_range_binding(range_uuid: UUID) -> tuple[TargetRef, ResourceScope]:
    """Return the exact SQL-owned parent and ancestry of one public range identity."""
    from engine.models import Range

    from ._range_authority import project_range_authority

    row = Range.objects.select_related("request").get(cms_range_uuid=range_uuid)
    if row.request is None:
        raise AuthorizationDescendantResolutionError("Range request unavailable")
    projection = project_range_authority(row.request.request_id)
    return projection.parent, projection.scope


def _authorization_range_targets(workspace_ids: Sequence[int], limit: int) -> tuple[TargetRef, ...]:
    """Project SQL-owned range identities into neutral authorization targets."""
    return tuple(TargetRef("range", item) for item in list_authorization_range_uuids(workspace_ids, limit))


def register_authorization_range_inventory() -> None:
    """Bind the Engine-owned SQL range inventory to the neutral port."""
    bind_authorization_descendant_resolver("engine.ranges", _authorization_range_targets)
    bind_authorization_event_range_resolver(_authorization_event_range_targets)
    bind_authorization_range_binding_resolver(_authorization_range_binding)
