"""Active-reference guard for tenant pack removal (ADR-034-R13, #2520).

A pack is referenced while any of these still depends on its catalog id:

- a CMS range in a non-terminal status (including pending, paused and
  destroying), whatever its soft-delete marker says;
- an operator warm-pool bucket that names it;
- a domain record reported through :mod:`shared.pack_references` (CTF events
  and launch-producing schedules).

Unverified cloud cleanup of a terminal range is not a pack reference: teardown
uses the range's immutable compiled plan and retained plugin pin, never the
pack. A blocker label is shown only when the record's owning domain lets the
actor read it; hidden records still count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import QuerySet

from cms.models import RangeInstance
from cms.services._range_workspace import authorized_range_workspace_ids
from shared.enums import TERMINAL_STATUSES
from shared.pack_references import PackReference, project_pack_references
from workspaces.services import WorkspaceOperation

if TYPE_CHECKING:
    from django.contrib.auth.models import User

#: Upper bound on blockers returned to the caller; the total is always exact.
BLOCKER_LIMIT = 20


@dataclass(frozen=True, slots=True)
class PackBlocker:
    """One bounded, disclosure-checked reference that blocks removal."""

    kind: str
    status: str
    label: str | None


@dataclass(frozen=True, slots=True)
class PackBlockers:
    """The blockers shown to the caller and the exact number of references."""

    items: tuple[PackBlocker, ...]
    total: int


def find_pack_blockers(scenario_id: str, actor: User) -> PackBlockers:
    """Return every active reference to ``scenario_id``, bounded for display.

    Raises:
        shared.pack_references.PackReferenceUnavailable: when a domain cannot
            establish its references; removal must then be refused.
    """
    buckets = _warm_pool_references(scenario_id)
    ranges = _active_ranges(scenario_id)
    readable = set(authorized_range_workspace_ids(actor, WorkspaceOperation.READ_RANGE))
    domain = project_pack_references(scenario_id, BLOCKER_LIMIT, actor)
    references = [
        *buckets,
        *(
            PackReference("range", status, str(uuid) if workspace_id in readable else None)
            for uuid, status, workspace_id in ranges.values_list("uuid", "status", "workspace_id")[:BLOCKER_LIMIT]
        ),
        *domain.references,
    ]
    total = len(buckets) + ranges.count() + domain.total
    return PackBlockers(
        tuple(PackBlocker(row.kind, row.status, row.label) for row in references[:BLOCKER_LIMIT]), total
    )


def _warm_pool_references(scenario_id: str) -> list[PackReference]:
    """An active operator warm-pool bucket keeps preparing ranges for its scenario.

    Bucket identifiers are operator configuration and are not disclosed.
    """
    policy = settings.WARM_POOL_POLICY
    if not policy.is_active():
        return []
    return [
        PackReference("warm_pool_bucket", "configured", None)
        for bucket in policy.buckets
        if bucket.scenario == scenario_id
    ]


def _active_ranges(scenario_id: str) -> QuerySet[RangeInstance]:
    """Non-terminal ranges, read through ``all_objects`` and decided by status."""
    terminal = [status.value for status in TERMINAL_STATUSES]
    return RangeInstance.all_objects.filter(scenario_id=scenario_id).exclude(status__in=terminal).order_by("pk")
