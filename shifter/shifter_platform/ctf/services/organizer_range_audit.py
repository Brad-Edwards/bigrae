"""Organizer attribution for per-participant range actions (#2617).

The range service acts as the participant, so the range's own audit row names
the participant. This event-scoped row names the requesting organizer and the
authority that admitted them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctf.services.audit import _entity_id_from_uuid
from shared.audit import (
    AuditAction,
    AuditEntityType,
    AuditEvent,
    audit_log,
    get_actor_from_request,
    get_client_ip,
    get_request_id,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest
    from rest_framework.request import Request

    from ctf.models import CTFEvent


def audit_organizer_participant_range_action(
    *,
    request: Request | HttpRequest,
    event: CTFEvent,
    participant_id: UUID,
    operation: str,
    authority_source: str,
) -> None:
    """Record which organizer ran a per-participant range action.

    The write is best effort, so the range action itself is unchanged.
    """
    actor_type, actor_id = get_actor_from_request(request)
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.CONFIG,
            entity_id=_entity_id_from_uuid(event.pk),
            entity_ref=str(event.pk),
            action=AuditAction.UPDATE,
            actor_type=actor_type,
            actor_id=actor_id,
            new_state={
                "operation": operation,
                "authority_source": authority_source,
                "event_id": str(event.pk),
                "participant_id": str(participant_id),
            },
            context="ctf_organizer_participant_range_action",
            source_ip=get_client_ip(request),
            request_id=get_request_id(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
        )
    )
