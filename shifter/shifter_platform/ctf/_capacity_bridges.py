"""CTF capacity integration through the public CMS service boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from shared.capacity import CapacityAssessmentResult


def cms_declare_event_capacity(
    *,
    event_ref: UUID,
    event_name: str,
    expected_concurrent_ranges: int,
    cohort_size: int,
    window_start: datetime | None,
    window_end: datetime | None,
    resource_hints: dict[str, Any],
) -> None:
    """Declare event capacity to the provisioning engine (CTF-908).

    Best-effort producer contract: the declaration is recorded before spinup
    so capacity-aware provisioning works from declared intent (#621); range
    provisioning itself never depends on this call succeeding.
    """
    import cms.services as cms_services

    signal = cms_services.EngineEventCapacitySignal(
        event_ref=event_ref,
        event_name=event_name,
        expected_concurrent_ranges=expected_concurrent_ranges,
        cohort_size=cohort_size,
        window_start=window_start,
        window_end=window_end,
        resource_hints=resource_hints,
    )
    cms_services.engine_record_capacity_declaration(signal)


def cms_assess_event_capacity(event_ref: UUID) -> CapacityAssessmentResult | None:
    """Ask the engine whether an event's declared capacity fits (PLAT-201).

    Best-effort consumer contract mirroring the declaration bridge: capacity
    admission informs provisioning, and a failure to assess must never be the
    reason an event cannot spin up. Returns the engine's assessment result, or
    ``None`` when the layer is disabled or no declaration exists.
    """
    import cms.services as cms_services

    return cms_services.engine_assess_declared_event_capacity(event_ref)


def cms_admit_range_capacity(event_ref: UUID, draw_key: UUID) -> CapacityAssessmentResult:
    """Draw one range's share from the event's capacity budget (PLAT-201).

    Pure database work in the engine -- no provider call -- so this is safe to
    run on the range-creation path. ``draw_key`` is the stable identity of the
    thing being provisioned (participant or spare), which is known before the
    range exists and makes a retried creation idempotent.
    """
    import cms.services as cms_services

    return cms_services.engine_admit_range_capacity(event_ref, draw_key=draw_key)


def cms_release_range_capacity(draw_key: UUID) -> int:
    """Return a range's capacity draw to its event budget (PLAT-201).

    Must run on every teardown path: a draw that outlives its range leaks
    capacity and eventually refuses an event that would have fit.
    """
    import cms.services as cms_services

    return cms_services.engine_release_range_capacity(draw_key)
