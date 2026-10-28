"""Pluggable domain-owned aggregate guard for range workspace-scope moves.

A range may belong to an immutable domain-owned workspace aggregate -- an
ADR-051 CTF event, or a future domain aggregate. Such a range's workspace scope
is owned by that aggregate and must not be moved independently by the Mission
Control workspace-scope administration surface (PLAT-237, #1944; ADR-046-R14).

CMS owns the rebind command but must not import a domain app to learn aggregate
membership (ADR-001). This seam mirrors :mod:`shared.range_visibility`: each
aggregate-owning domain registers one guard at app startup, and CMS consults the
union through :func:`range_instance_ids_in_domain_aggregates`.

Membership is decided authoritatively by the registered guards, never by a
range's provenance label. The seam fails CLOSED: a guard that raises is treated
as "cannot establish absence", so its candidate ranges are reported as
aggregate-bound rather than released. With no guard registered, no domain owns
aggregates, so nothing is bound.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable
    from uuid import UUID

    from shared.credentials import CredentialContext


logger = logging.getLogger(__name__)

# Guard contract: given (request_id, range_instance_id) pairs, return the subset
# of range_instance_ids the domain owns in an immutable aggregate.
RangeAggregateGuard = Callable[[Sequence[tuple["uuid.UUID", int]]], "Iterable[int]"]

_guards: list[RangeAggregateGuard] = []
RangeEventResolver = Callable[["uuid.UUID", int], "uuid.UUID | None"]
_event_resolver: RangeEventResolver | None = None


class RangeAssociationValidator(Protocol):
    """Owner callback that can fence the association within an existing transaction."""

    def __call__(self, request_id: UUID, event_uuid: UUID, *, for_update: bool = False) -> None:
        """Validate the exact range/event association with optional transaction locking."""
        ...


_event_association_validator: RangeAssociationValidator | None = None
_event_launch_validator: Callable[[UUID, UUID], None] | None = None
_vpn_participation = None


def register_range_vpn_participation(resolver: Callable[[CredentialContext, UUID, UUID | None], None]) -> None:
    """CTF owns admission to its live participant/range assignment."""
    global _vpn_participation
    if not callable(resolver) or (_vpn_participation is not None and _vpn_participation is not resolver):
        raise ValueError("Range participation resolver already bound")
    _vpn_participation = resolver


def require_range_vpn_participation(credential: CredentialContext, request_id: UUID, event_uuid: UUID | None) -> None:
    """Consult the aggregate owner without importing its models or services."""
    if _vpn_participation is None:
        raise ValueError("Range participation unavailable")
    _vpn_participation(credential, request_id, event_uuid)


def register_range_event_resolver(resolver: RangeEventResolver) -> None:
    """Extend the incumbent aggregate seam with exact CTF parent identity."""
    global _event_resolver
    if not callable(resolver) or (_event_resolver is not None and _event_resolver is not resolver):
        raise ValueError("Range event resolver already bound")
    _event_resolver = resolver


def range_event_parent(request_id: uuid.UUID, range_instance_id: int) -> uuid.UUID | None:
    """Require the aggregate owner's exact SQL event, including spare ranges."""
    if _event_resolver is None:
        raise ValueError("Range event binding unavailable")
    return _event_resolver(request_id, range_instance_id)


def register_range_event_association_validator(validator: RangeAssociationValidator) -> None:
    """The aggregate owner separates reserved intent from dispatch readiness."""
    global _event_association_validator
    if not callable(validator) or (
        _event_association_validator is not None and _event_association_validator is not validator
    ):
        raise ValueError("Range event association validator already bound")
    _event_association_validator = validator


def require_range_event_association(request_id: UUID, event_uuid: UUID) -> None:
    """A reserved request alone cannot authorize runnable event work."""
    if _event_association_validator is None:
        raise ValueError("Range event association unavailable")
    _event_association_validator(request_id, event_uuid)


def fence_range_event_association(request_id: UUID, event_uuid: UUID) -> None:
    """Fence association changes against cancellation before other row locks."""
    if _event_association_validator is None:
        raise ValueError("Range event association unavailable")
    _event_association_validator(request_id, event_uuid, for_update=True)


def register_range_event_launch_validator(validator: Callable[[UUID, UUID], None]) -> None:
    """The event owner serializes cancellation with creation across layers."""
    global _event_launch_validator
    if not callable(validator) or (_event_launch_validator is not None and _event_launch_validator is not validator):
        raise ValueError("Range event launch validator already bound")
    _event_launch_validator = validator


def fence_range_event_launch(request_id: UUID, event_uuid: UUID) -> None:
    """Caller holds a transaction; the event lock precedes CMS/native locks."""
    if _event_launch_validator is None:
        raise ValueError("Range event launch unavailable")
    _event_launch_validator(request_id, event_uuid)


def register_range_aggregate_guard(guard: RangeAggregateGuard) -> None:
    """Register a domain aggregate guard (idempotent by identity)."""
    if not callable(guard):
        raise TypeError("guard must be callable")
    if guard not in _guards:
        _guards.append(guard)


def range_instance_ids_in_domain_aggregates(pairs: Iterable[tuple[uuid.UUID, int]]) -> set[int]:
    """Return the ``range_instance_id`` values in ``pairs`` bound to a domain aggregate.

    Unions every registered guard. Fails closed: if a guard raises, every
    candidate range it was asked about is reported bound, because the guard's
    domain could not establish absence.
    """
    materialized = [
        (request_id, range_instance_id) for request_id, range_instance_id in pairs if range_instance_id is not None
    ]
    if not materialized:
        return set()
    bound: set[int] = set()
    for guard in _guards:
        try:
            bound.update(int(range_instance_id) for range_instance_id in guard(materialized))
        except Exception:
            logger.exception("range aggregate guard failed; failing closed for its candidate ranges")
            bound.update(range_instance_id for _, range_instance_id in materialized)
    return bound


def range_in_domain_aggregate(request_id: uuid.UUID, range_instance_id: int) -> bool:
    """Return whether a single range is bound to a domain-owned aggregate."""
    return range_instance_id in range_instance_ids_in_domain_aggregates([(request_id, range_instance_id)])
