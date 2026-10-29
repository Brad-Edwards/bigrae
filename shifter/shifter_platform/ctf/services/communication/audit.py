"""Strict, body-free authority evidence for scoped communication operations."""

from uuid import UUID

from ctf.services._audit_attribution import _ActorFields, _entity_id_from_uuid
from shared.audit import AuditAction, AuditActorType, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext


def audit_communication_authority(
    *,
    event_id: UUID,
    actor_id: int | CredentialContext,
    token_id: int | None,
    authority_source: str,
    request_id: str = "",
    operation: str = "admit",
) -> None:
    """Persist each target's live authority, including delegated and override use."""
    if isinstance(actor_id, CredentialContext):
        actor_fields: _ActorFields = {**principal_actor_fields(actor_id.principal)}
    else:
        actor_fields = {
            "actor_type": AuditActorType.APIKEY if token_id is not None else AuditActorType.USER,
            "actor_id": token_id if token_id is not None else actor_id,
        }
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.COMMUNICATION,
            entity_id=_entity_id_from_uuid(event_id),
            entity_ref=str(event_id),
            action=AuditAction.UPDATE,
            **actor_fields,
            new_state={
                "event_id": str(event_id),
                "effective_actor_principal_uuid": (
                    str(actor_id.principal.uuid) if isinstance(actor_id, CredentialContext) else None
                ),
                "authority_source": authority_source,
                "operation": operation,
            },
            context="ctf_communication_authority",
            request_id=request_id,
        ),
        strict=True,
    )
