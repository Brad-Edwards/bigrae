"""Closed actor attribution and stable identifiers for CTF audit events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict
from uuid import UUID

from shared.audit import AuditAction, AuditActorType


class _ActorFields(TypedDict):
    """Closed actor attribution fields shared by CTF audit records."""

    actor_type: str
    actor_id: int | None
    actor_principal_uuid: NotRequired[UUID]


def _actor_fields(actor: object | None) -> _ActorFields:
    """Return canonical human, service-principal, or system attribution."""
    from django.contrib.auth import get_user_model

    from ctf.services.actors import EventActionAdmission
    from shared.credentials import CredentialContext

    if isinstance(actor, EventActionAdmission):
        actor = actor.credential
    if isinstance(actor, CredentialContext):
        from shared.audit import principal_actor_fields

        fields: _ActorFields = {**principal_actor_fields(actor.principal)}
    elif isinstance(actor, int):
        fields = {"actor_type": AuditActorType.USER, "actor_id": actor}
    elif isinstance(actor, get_user_model()):
        from ctf.services.actors import event_actor_credential

        credential = event_actor_credential(actor)
        fields = {
            "actor_type": AuditActorType.USER,
            "actor_id": actor.pk,
            "actor_principal_uuid": credential.principal.uuid,
        }
    elif actor is None:
        fields = {"actor_type": AuditActorType.SYSTEM, "actor_id": None}
    else:
        raise ValueError("Invalid CTF audit actor")
    return fields


def _entity_id_from_uuid(entity_uuid: UUID) -> int:
    """Map a UUID to a positive int for AuditLog.entity_id."""
    return int.from_bytes(entity_uuid.bytes[:4], "big") % (2**31 - 1) or 1


@dataclass(frozen=True)
class EventMutationOutcome:
    """Bounded outcome metadata attached to an admitted event mutation."""

    action: str = AuditAction.UPDATE
    changed_fields: tuple[str, ...] = ()
    outcome: str | None = None
