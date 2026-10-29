"""CTF domain-owned range aggregate guard (PLAT-237, #1944; ADR-046-R14, ADR-051).

A range is part of a CTF event aggregate when a CTF team holds it as its live
range, or a spare-range pool row owns it for an event. Such a range's workspace
scope is bound to the event and must not be moved independently by the Mission
Control workspace-scope administration surface. Registered into the shared
aggregate-guard seam at app startup so CMS can consult it without importing CTF
(ADR-001); membership is decided from CTF's own rows, never from a provenance
label carried on the range projection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence


def ctf_range_aggregate_guard(pairs: Sequence[tuple[uuid.UUID, int]]) -> set[int]:
    """Return the ``range_instance_id`` values in ``pairs`` bound to a CTF event."""
    from ctf.models import CTFParticipant, CTFRangeLaunch, CTFRangeRecovery

    request_to_range = {
        request_id: range_instance_id for request_id, range_instance_id in pairs if range_instance_id is not None
    }
    range_ids = set(request_to_range.values())
    request_ids = set(request_to_range)
    if not range_ids:
        return set()

    # The model columns are nullable, so guard each row against None even though
    # an ``__in`` filter over non-null ids can only return non-null matches.
    bound: set[int] = set()
    for range_instance_id in CTFParticipant.objects.filter(range_instance_id__in=range_ids).values_list(
        "range_instance_id", flat=True
    ):
        if range_instance_id is not None:
            bound.add(range_instance_id)
    bound.update(_spare_aggregate_bindings(request_to_range))
    bound.update(
        instance_id
        for instance_id in CTFRangeRecovery.objects.filter(
            replacement_range_instance_id__in=range_ids,
            replacement_request_id__in=request_ids,
        ).values_list("replacement_range_instance_id", flat=True)
        if instance_id is not None
    )
    bound.update(
        request_to_range[request_id]
        for request_id in CTFRangeLaunch.all_objects.filter(pk__in=request_ids).values_list("pk", flat=True)
    )
    return bound


def ctf_range_event_parent(
    request_id: uuid.UUID, range_instance_id: int, *, include_pending: bool = True
) -> uuid.UUID | None:
    """Resolve actual event membership; same-account events are not aliases."""
    from django.db.models import Q

    from ctf.models import CTFParticipant, CTFRangeLaunch, CTFRangeRecovery, CTFSpareRange

    events = set(
        CTFParticipant.all_objects.filter(range_instance_id=range_instance_id).values_list("event_id", flat=True)[:2]
    )
    spare_filter = Q(range_instance_id=range_instance_id)
    if include_pending:
        spare_filter |= Q(request_id=request_id)
    events.update(CTFSpareRange.all_objects.filter(spare_filter).values_list("event_id", flat=True)[:2])
    events.update(
        CTFRangeRecovery.objects.filter(
            replacement_range_instance_id=range_instance_id,
            replacement_request_id=request_id,
        ).values_list("event_id", flat=True)[:2]
    )
    if include_pending:
        events.update(CTFRangeLaunch.all_objects.filter(pk=request_id).values_list("event_id", flat=True))
    if len(events) > 1:
        raise ValueError("Range event binding unavailable")
    return next(iter(events), None)


def require_ctf_range_association(request_id: UUID, event_uuid: UUID, *, for_update: bool = False) -> None:
    """Only an exact live participant/spare/replacement pointer may dispatch."""
    from ctf.bridges import cms_find_range_instance_id
    from ctf.models import CTFEvent, CTFRangeLaunch

    if for_update:
        CTFEvent.objects.select_for_update().get(pk=event_uuid)
        # Lock the owning journal when present before consulting its state.
        list(CTFRangeLaunch.all_objects.select_for_update().filter(pk=request_id, event_id=event_uuid))

    instance_id = cms_find_range_instance_id(request_id)
    if (
        CTFRangeLaunch.all_objects.filter(pk=request_id, deleted_at__isnull=False).exists()
        or instance_id is None
        or ctf_range_event_parent(request_id, instance_id, include_pending=False) != event_uuid
    ):
        raise ValueError("Range event association unavailable")


def register_ctf_range_aggregate_guard() -> None:
    """Register the CTF aggregate guard with the shared seam (idempotent)."""
    from shared.range_workspace_aggregate import (
        register_range_aggregate_guard,
        register_range_event_association_validator,
        register_range_event_launch_validator,
        register_range_event_resolver,
    )

    register_range_aggregate_guard(ctf_range_aggregate_guard)
    register_range_event_resolver(ctf_range_event_parent)
    register_range_event_association_validator(require_ctf_range_association)
    from ctf.services.range.launch_intents import fence_launch_intent

    register_range_event_launch_validator(fence_launch_intent)


def _spare_aggregate_bindings(request_to_range: dict[UUID, int]) -> set[int]:
    """Retained spare custody binds both its exact instance and reserved request identities."""
    from ctf.models import CTFSpareRange

    range_ids = set(request_to_range.values())
    request_ids = set(request_to_range)
    bound: set[int] = set()
    for range_instance_id in CTFSpareRange.objects.filter(range_instance_id__in=range_ids).values_list(
        "range_instance_id", flat=True
    ):
        if range_instance_id is not None:
            bound.add(range_instance_id)
    for request_id in CTFSpareRange.objects.filter(request_id__in=request_ids).values_list("request_id", flat=True):
        if request_id is None:
            continue
        range_instance_id = request_to_range.get(request_id)
        if range_instance_id is not None:
            bound.add(range_instance_id)
    return bound
