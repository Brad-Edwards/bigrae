"""Durable publication of event ancestry and initial administrator authority."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from ctf.exceptions import CTFStateError
from ctf.models import CTFEvent
from shared.authorization import CutoverRelationshipChange
from shared.authorization.port import configured_authorization_provider
from workspaces.services import resource_scope_from_ids

_UNAVAILABLE = "Event authority publication is unavailable"


@dataclass(frozen=True, slots=True)
class _PublicationSnapshot:
    event_uuid: UUID
    owner_uuid: UUID
    parent_kind: str
    parent_uuid: UUID
    store_id: str
    model_id: str


def _snapshot(event: CTFEvent, store_id: str, model_id: str) -> _PublicationSnapshot:
    """Resolve the immutable event relationship intent from SQL-owned ancestry."""
    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    if scope.workspace_uuid is not None:
        parent_kind, parent_uuid = "workspace", scope.workspace_uuid
    elif scope.organization_uuid is not None:
        parent_kind, parent_uuid = "organization", scope.organization_uuid
    elif scope.account_uuid is not None:
        parent_kind, parent_uuid = "account", scope.account_uuid
    else:
        raise ValueError("event parent unavailable")
    return _PublicationSnapshot(event.pk, event.owner_principal_uuid, parent_kind, parent_uuid, store_id, model_id)


def _changes(snapshot: _PublicationSnapshot) -> tuple[CutoverRelationshipChange, CutoverRelationshipChange]:
    """Return the exact parent and creator-administrator grants for one event."""
    target = f"event:{snapshot.event_uuid}"
    return (
        CutoverRelationshipChange(f"{snapshot.parent_kind}:{snapshot.parent_uuid}", snapshot.parent_kind, target),
        CutoverRelationshipChange(f"principal:{snapshot.owner_uuid}", "direct_administrator", target),
    )


def _write_and_readback(provider, change: CutoverRelationshipChange) -> bool:
    """Recover a lost write acknowledgement through primary relationship readback."""
    with suppress(Exception):
        provider.write_relationships(change)
    try:
        observed = provider.read_relationships(change)
    except Exception:
        return False
    return bool(observed.grant_present)


def _mark_unresolved(event_uuid: UUID, reason: str) -> None:
    """Retain the event and its exact intent for an idempotent later retry."""
    CTFEvent.all_objects.filter(pk=event_uuid).exclude(authorization_publication_state="confirmed").update(
        authorization_publication_state="unresolved",
        authorization_publication_reason=reason,
    )


def publish_event_relationships(event_uuid: UUID) -> CTFEvent:
    """Publish and verify event relationships without provider I/O under SQL locks."""
    try:
        provider = configured_authorization_provider()
        store_id = str(provider.store_id)
        model_id = str(provider.model_id)
        if not store_id or not model_id:
            raise ValueError("provider binding unavailable")
        with transaction.atomic():
            event = CTFEvent.all_objects.select_for_update().get(pk=event_uuid)
            if event.authorization_publication_state == "confirmed":
                if event.authorization_provider_store_id and (
                    event.authorization_provider_store_id != store_id or event.authorization_model_id != model_id
                ):
                    raise ValueError("provider binding changed")
                return event
            if event.authorization_provider_store_id and (
                event.authorization_provider_store_id != store_id or event.authorization_model_id != model_id
            ):
                raise ValueError("provider binding changed")
            snapshot = _snapshot(event, store_id, model_id)
            event.authorization_publication_state = "requested"
            event.authorization_provider_store_id = store_id
            event.authorization_model_id = model_id
            event.authorization_publication_reason = "accepted"
            event.save(
                update_fields=[
                    "authorization_publication_state",
                    "authorization_provider_store_id",
                    "authorization_model_id",
                    "authorization_publication_reason",
                    "updated_at",
                ]
            )
        if not all(_write_and_readback(provider, change) for change in _changes(snapshot)):
            _mark_unresolved(event_uuid, "provider_outcome_unknown")
            raise ValueError("relationship readback failed")
        with transaction.atomic():
            event = CTFEvent.all_objects.select_for_update().get(pk=event_uuid)
            if _snapshot(event, store_id, model_id) != snapshot:
                event.authorization_publication_state = "unresolved"
                event.authorization_publication_reason = "event_binding_changed"
                event.save(
                    update_fields=[
                        "authorization_publication_state",
                        "authorization_publication_reason",
                        "updated_at",
                    ]
                )
                raise ValueError("event binding changed")
            event.authorization_publication_state = "confirmed"
            event.authorization_publication_reason = "primary_readback_confirmed"
            event.save(
                update_fields=[
                    "authorization_publication_state",
                    "authorization_publication_reason",
                    "updated_at",
                ]
            )
            return event
    except CTFStateError:
        raise
    except Exception:
        _mark_unresolved(event_uuid, "provider_outcome_unknown")
        raise CTFStateError(_UNAVAILABLE, code="CTF_EVENT_AUTHORITY_UNAVAILABLE") from None
