"""Organizer attribution for per-participant range actions (#2617).

This event-scoped row supplements the owning range service's native principal
audit with the requested participant and the policy authority that admitted it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctf.services.audit import _entity_id_from_uuid
from shared.audit import (
    AuditAction,
    AuditEntityType,
    AuditEvent,
    audit_log,
    request_audit,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest
    from rest_framework.request import Request

    from ctf.models import CTFEvent
    from shared.credentials import CredentialContext


def audit_organizer_participant_range_action(
    *,
    request: Request | HttpRequest,
    credential: CredentialContext,
    event: CTFEvent,
    participant_id: UUID,
    operation: str,
    authority_source: str,
) -> None:
    """Record which organizer ran a per-participant range action.

    The write is best effort, so the range action itself is unchanged.
    """
    attribution = request_audit(request, credential=credential)
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.CONFIG,
            entity_id=_entity_id_from_uuid(event.pk),
            entity_ref=str(event.pk),
            action=AuditAction.UPDATE,
            actor_type=attribution.actor_type,
            actor_id=attribution.actor_id,
            actor_principal_uuid=attribution.actor_principal_uuid,
            new_state={
                "operation": operation,
                "authority_source": authority_source,
                "event_id": str(event.pk),
                "participant_id": str(participant_id),
            },
            context="ctf_organizer_participant_range_action",
            source_ip=attribution.source_ip,
            request_id=attribution.request_id,
            user_agent=attribution.user_agent[:255],
        )
    )
