"""CTF-owned event ancestry projection for credential authority checks."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID

from ctf.models import CTFEvent
from shared.identity_scope import ResourceScope
from workspaces.services import event_placement_scope, resource_scope_from_ids

if TYPE_CHECKING:
    from collections.abc import Iterator

_SCOPE_UNAVAILABLE = "Credential scope unavailable"
_INITIALIZING_EVENT: ContextVar[tuple[UUID, UUID] | None] = ContextVar("ctf_initializing_event", default=None)
_CLEANUP_EVENT: ContextVar[UUID | None] = ContextVar("ctf_cleanup_event", default=None)


@contextmanager
def _retained_event_cleanup_scope(event_uuid: UUID) -> Iterator[None]:
    """Expose retained ancestry only during deletion; CMS still checks live event.delete."""
    token = _CLEANUP_EVENT.set(event_uuid)
    try:
        yield
    finally:
        _CLEANUP_EVENT.reset(token)


@contextmanager
def _event_initialization_scope(event_uuid: UUID, principal_uuid: UUID) -> Iterator[None]:
    """Admit only the original creator's internal initialization in this context."""
    token = _INITIALIZING_EVENT.set((event_uuid, principal_uuid))
    try:
        yield
    finally:
        _INITIALIZING_EVENT.reset(token)


def _event_scope(event: CTFEvent | None) -> ResourceScope:
    """Validate one retained event's SQL ancestry."""
    if event is None or event.authorization_publication_state != "confirmed":
        raise ValueError(_SCOPE_UNAVAILABLE)
    if event.initialization_intent is not None and _INITIALIZING_EVENT.get() != (event.pk, event.owner_principal_uuid):
        raise ValueError(_SCOPE_UNAVAILABLE)
    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    if scope.kind == "account":
        if scope.workspace_uuid is not None:
            parent_type, parent_uuid = "workspace", scope.workspace_uuid
        elif scope.organization_uuid is not None:
            parent_type, parent_uuid = "organization", scope.organization_uuid
        else:
            if scope.account_uuid is None:
                raise ValueError(_SCOPE_UNAVAILABLE)
            parent_type, parent_uuid = "account", scope.account_uuid
        if event_placement_scope(parent_type, parent_uuid) != scope:
            raise ValueError(_SCOPE_UNAVAILABLE)
    return scope


def event_credential_scope(event_uuid: UUID) -> ResourceScope:
    """Return live validated SQL ancestry, never an absent-scope installation default."""
    if _CLEANUP_EVENT.get() == event_uuid:
        return retained_event_delete_scope(event_uuid)
    return _event_scope(CTFEvent.objects.filter(pk=event_uuid, deleted_at__isnull=True).first())


def retained_event_delete_scope(event_uuid: UUID) -> ResourceScope:
    """Resolve retained tombstone ancestry solely for permanent event deletion."""
    return _event_scope(CTFEvent.all_objects.filter(pk=event_uuid).first())
