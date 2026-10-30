"""Neutral binding for SQL-authoritative authorization descendant inventory."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from shared.identity_scope import ResourceScope

from .contracts import TargetRef

_AUTHORIZATION_RANGE_BINDING_UNAVAILABLE = "Authorization range binding unavailable"


class AuthorizationDescendantResolutionError(RuntimeError):
    """The authoritative descendant inventory is unavailable or exceeds its bound."""


AuthorizationDescendantResolver = Callable[[Sequence[int], int], tuple[TargetRef, ...]]
_REQUIRED_RESOLVERS = {"ctf.events": "event", "engine.ranges": "range"}
_resolvers: dict[str, AuthorizationDescendantResolver] = {}
AuthorizationEventScopeResolver = Callable[[UUID], ResourceScope]
AuthorizationNonworkspaceEventResolver = Callable[[ResourceScope, int], tuple[tuple[TargetRef, ResourceScope], ...]]
_event_scope_resolver: AuthorizationEventScopeResolver | None = None
_nonworkspace_event_resolver: AuthorizationNonworkspaceEventResolver | None = None
_event_range_resolver: Callable[[UUID, int], tuple[TargetRef, ...]] | None = None
_range_binding_resolver: Callable[[UUID], tuple[TargetRef, ResourceScope]] | None = None
_EVENT_INVENTORY_UNAVAILABLE = "Authorization event inventory is unavailable"


def bind_authorization_event_inventory(
    scope_resolver: AuthorizationEventScopeResolver,
    nonworkspace_resolver: AuthorizationNonworkspaceEventResolver,
) -> None:
    """Bind CTF-owned exact event scope and account/organization event inventory."""
    global _event_scope_resolver, _nonworkspace_event_resolver
    if not callable(scope_resolver) or not callable(nonworkspace_resolver):
        raise AuthorizationDescendantResolutionError("Authorization event inventory binding is invalid")
    if (_event_scope_resolver is not None and _event_scope_resolver is not scope_resolver) or (
        _nonworkspace_event_resolver is not None and _nonworkspace_event_resolver is not nonworkspace_resolver
    ):
        raise AuthorizationDescendantResolutionError("A different resolver owns the event inventory")
    _event_scope_resolver = scope_resolver
    _nonworkspace_event_resolver = nonworkspace_resolver


def resolve_authorization_event_scope(event_uuid: UUID) -> ResourceScope:
    """Resolve an event's exact SQL ancestry, failing closed on missing data."""
    if _event_scope_resolver is None:
        raise AuthorizationDescendantResolutionError(_EVENT_INVENTORY_UNAVAILABLE)
    try:
        scope = _event_scope_resolver(event_uuid)
    except Exception as exc:
        raise AuthorizationDescendantResolutionError("Authorization event scope is unavailable") from exc
    if not isinstance(scope, ResourceScope):
        raise AuthorizationDescendantResolutionError("Authorization event scope is invalid")
    return scope


def bind_authorization_event_range_resolver(resolver: Callable[[UUID, int], tuple[TargetRef, ...]]) -> None:
    """Bind the native range owner's SQL inventory below an event."""
    global _event_range_resolver
    if not callable(resolver) or (_event_range_resolver is not None and _event_range_resolver is not resolver):
        raise AuthorizationDescendantResolutionError("Authorization event range inventory binding is invalid")
    _event_range_resolver = resolver


def bind_authorization_range_binding_resolver(resolver: Callable[[UUID], tuple[TargetRef, ResourceScope]]) -> None:
    """Bind native SQL custody for public range policy targets."""
    global _range_binding_resolver
    if not callable(resolver) or (_range_binding_resolver is not None and _range_binding_resolver is not resolver):
        raise AuthorizationDescendantResolutionError(_AUTHORIZATION_RANGE_BINDING_UNAVAILABLE)
    _range_binding_resolver = resolver


def resolve_authorization_range_binding(range_uuid: UUID) -> tuple[TargetRef, ResourceScope]:
    """Retain event ancestry instead of deriving it from a workspace alone."""
    if _range_binding_resolver is None:
        raise AuthorizationDescendantResolutionError(_AUTHORIZATION_RANGE_BINDING_UNAVAILABLE)
    try:
        parent, scope = _range_binding_resolver(range_uuid)
        if not isinstance(parent, TargetRef) or not isinstance(scope, ResourceScope):
            raise ValueError("Invalid range binding")
        return parent, scope
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise AuthorizationDescendantResolutionError(_AUTHORIZATION_RANGE_BINDING_UNAVAILABLE) from None


def resolve_authorization_event_ranges(event_uuid: UUID, limit: int) -> tuple[tuple[TargetRef, ResourceScope], ...]:
    """Bound all existing event children, using their public authorization IDs."""
    if _event_range_resolver is None or isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise AuthorizationDescendantResolutionError("Authorization event range inventory unavailable")
    scope = resolve_authorization_event_scope(event_uuid)
    try:
        rows = _event_range_resolver(event_uuid, limit + 1)
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise AuthorizationDescendantResolutionError("Authorization event range inventory unavailable") from None
    if len(rows) > limit or len(set(rows)) != len(rows) or any(row.type != "range" for row in rows):
        raise AuthorizationDescendantResolutionError("Authorization event range inventory invalid")
    return tuple((row, scope) for row in rows)


def _valid_nonworkspace_inventory_request(parent: ResourceScope, limit: int) -> bool:
    """Accept only bounded account or installation ancestry requests."""
    return (
        isinstance(parent, ResourceScope)
        and parent.workspace_uuid is None
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and limit >= 0
    )


def _valid_nonworkspace_event(parent: ResourceScope, target: TargetRef, scope: ResourceScope) -> bool:
    """Require each returned event to remain at the requested SQL ancestry."""
    return (
        target.type == "event"
        and scope.kind == "account"
        and scope.workspace_uuid is None
        and (parent.kind != "account" or scope.account_uuid == parent.account_uuid)
        and (parent.organization_uuid is None or scope.organization_uuid == parent.organization_uuid)
    )


def resolve_authorization_nonworkspace_events(
    parent: ResourceScope, limit: int
) -> tuple[tuple[TargetRef, ResourceScope], ...]:
    """Return bounded events placed directly in an account or organization."""
    if _nonworkspace_event_resolver is None:
        raise AuthorizationDescendantResolutionError(_EVENT_INVENTORY_UNAVAILABLE)
    if not _valid_nonworkspace_inventory_request(parent, limit):
        raise AuthorizationDescendantResolutionError("Authorization event inventory request is invalid")
    try:
        rows = _nonworkspace_event_resolver(parent, limit + 1)
    except Exception as exc:
        raise AuthorizationDescendantResolutionError(_EVENT_INVENTORY_UNAVAILABLE) from exc
    if len(rows) > limit or not all(_valid_nonworkspace_event(parent, target, scope) for target, scope in rows):
        raise AuthorizationDescendantResolutionError("Authorization event inventory is invalid")
    return rows


def bind_authorization_descendant_resolver(name: str, resolver: AuthorizationDescendantResolver) -> None:
    """Bind one SQL-owning domain's descendant resolver once."""
    if name not in _REQUIRED_RESOLVERS or not callable(resolver):
        raise AuthorizationDescendantResolutionError("Authorization descendant resolver binding is invalid")
    existing = _resolvers.get(name)
    if existing is not None and existing is not resolver:
        raise AuthorizationDescendantResolutionError(f"A different authorization descendant resolver owns {name}")
    _resolvers[name] = resolver


def resolve_authorization_descendants(workspace_ids: Sequence[int], limit: int) -> tuple[TargetRef, ...]:
    """Return bounded event/range targets from their SQL-owning domains."""
    if set(_resolvers) != set(_REQUIRED_RESOLVERS):
        raise AuthorizationDescendantResolutionError("Authorization descendant resolver is unavailable")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise AuthorizationDescendantResolutionError("Authorization descendant limit is invalid")
    descendants: list[TargetRef] = []
    for name in sorted(_resolvers):
        remaining = limit - len(descendants)
        try:
            resolved = _resolvers[name](tuple(workspace_ids), remaining + 1)
        except AuthorizationDescendantResolutionError:
            raise
        except Exception as exc:
            raise AuthorizationDescendantResolutionError("Authorization descendant resolution failed") from exc
        if len(resolved) > remaining or any(item.type != _REQUIRED_RESOLVERS[name] for item in resolved):
            raise AuthorizationDescendantResolutionError("Authorization descendant resolution is invalid")
        descendants.extend(resolved)
    return tuple(descendants)


@dataclass(frozen=True, slots=True)
class EventAuthorityEffect:
    """One immutable domain-owned effect; its semantic key is never a list index."""

    key: str
    subject_uuid: UUID
    effect: str
    action: str = ""
    policy_code: str = ""


@dataclass(frozen=True, slots=True)
class RetainedEventAuthorityPlan:
    """Exact SQL-owned admission for one pending staff or ownership command."""

    transition_uuid: UUID
    event_uuid: UUID
    kind: str
    target_uuid: UUID
    previous_owner_uuid: UUID | None
    scope: ResourceScope
    effects: tuple[EventAuthorityEffect, ...]
    store_id: str
    model_id: str

    def __post_init__(self) -> None:
        if (
            self.kind not in {"assign", "revoke", "transfer"}
            or not 1 <= len(self.effects) <= 100
            or len({effect.key for effect in self.effects}) != len(self.effects)
            or (self.previous_owner_uuid is not None) != (self.kind == "transfer")
            or not self.store_id
            or not self.model_id
        ):
            raise ValueError("Retained event authority plan is invalid")


_authority_plan_resolver: Callable[[UUID], RetainedEventAuthorityPlan] | None = None


def bind_event_authority_plan_resolver(resolver: Callable[[UUID], RetainedEventAuthorityPlan]) -> None:
    """Bind the aggregate owner's retained admission projection once."""
    global _authority_plan_resolver
    if not callable(resolver) or (_authority_plan_resolver is not None and _authority_plan_resolver is not resolver):
        raise AuthorizationDescendantResolutionError("Event authority plan resolver already bound")
    _authority_plan_resolver = resolver


def resolve_event_authority_plan(transition_uuid: UUID) -> RetainedEventAuthorityPlan:
    """Require live persisted admission; caller declarations confer no authority."""
    if _authority_plan_resolver is None:
        raise AuthorizationDescendantResolutionError("Event authority plan unavailable")
    result = _authority_plan_resolver(transition_uuid)
    if not isinstance(result, RetainedEventAuthorityPlan) or result.transition_uuid != transition_uuid:
        raise AuthorizationDescendantResolutionError("Event authority plan unavailable")
    return result
