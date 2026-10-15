"""Neutral, fail-closed port for domain-owned pack reference projections (ADR-034-R13).

CMS removes tenant packs but must not import a domain app to learn which of its
records still reference a pack's catalog id (ADR-001). Each reference-owning
domain binds one projection at app startup; CMS consults the union through
:func:`project_pack_references`.

The port fails CLOSED: with no binding, or when a projection raises or returns
an invalid shape, references cannot be ruled out, so removal is unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class PackReferenceUnavailable(RuntimeError):
    """Pack references could not be established; removal must not proceed."""


@dataclass(frozen=True, slots=True)
class PackReference:
    """One domain record that still depends on a pack's catalog id.

    ``label`` is the record's display name, or ``None`` when the owning domain's
    own authorization does not let the requesting actor see the record. A hidden
    record still blocks removal.
    """

    kind: str
    status: str
    label: str | None


@dataclass(frozen=True, slots=True)
class PackReferenceSet:
    """A bounded projection: at most ``limit`` references plus the true total."""

    references: tuple[PackReference, ...]
    total: int


# Projection contract: (scenario_id, limit, actor) -> bounded references and total
# count, with labels disclosed only under the domain's authorization for ``actor``.
PackReferenceProjection = Callable[[str, int, Any], PackReferenceSet]

_projections: dict[str, PackReferenceProjection] = {}
_REQUIRED_PROJECTIONS = frozenset({"ctf"})


def bind_pack_reference_projection(name: str, projection: PackReferenceProjection) -> None:
    """Bind one domain's projection once; a different callable is a conflict."""
    if name not in _REQUIRED_PROJECTIONS or not callable(projection):
        raise PackReferenceUnavailable("Pack reference projection binding is invalid")
    existing = _projections.get(name)
    if existing is not None and existing is not projection:
        raise PackReferenceUnavailable(f"A different pack reference projection owns {name}")
    _projections[name] = projection


def project_pack_references(scenario_id: str, limit: int, actor: Any) -> PackReferenceSet:
    """Return the union of every domain's bounded references, failing closed."""
    if set(_projections) != _REQUIRED_PROJECTIONS:
        raise PackReferenceUnavailable("Pack reference projection is unavailable")
    references: list[PackReference] = []
    total = 0
    for name in sorted(_projections):
        try:
            result = _projections[name](scenario_id, limit, actor)
        except Exception as exc:
            raise PackReferenceUnavailable("Pack reference projection failed") from exc
        if (
            not isinstance(result, PackReferenceSet)
            or len(result.references) > limit
            or result.total < len(result.references)
            or not all(isinstance(row, PackReference) for row in result.references)
        ):
            raise PackReferenceUnavailable("Pack reference projection is invalid")
        references.extend(result.references)
        total += result.total
    return PackReferenceSet(tuple(references[:limit]), total)
