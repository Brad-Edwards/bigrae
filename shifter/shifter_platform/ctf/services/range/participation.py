"""Live event standing and exact assignment for participant range access."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from shared.credentials import CredentialContext


def require_assigned_range_participation(
    credential: CredentialContext, request_id: UUID, event_uuid: UUID | None
) -> None:
    """Require live policy, participation and the exact native range assignment."""
    from ctf.bridges import cms_find_range_instance_id
    from ctf.services.principal_participation import participant_for_credential

    if event_uuid is None:
        raise ValueError("Participation denied")
    participant = participant_for_credential(credential, event_uuid)
    instance_id = cms_find_range_instance_id(request_id)
    if (
        instance_id is None
        or participant.range_instance_id != instance_id
        or participant.range_request_id != request_id
    ):
        raise ValueError("Participation denied")


def admit_owned_range_remote_participation(
    credential: CredentialContext, request_id: UUID, event_uuid: UUID, owner_uuid: UUID
) -> None:
    """Fence participant-owned access while preserving independent operator policy."""
    from ctf.models import CTFParticipant

    if credential.principal.uuid != owner_uuid:
        return
    # Retained participation must not become unrestricted operator access when
    # a ban, soft deletion or assignment removal invalidates the live predicate.
    if CTFParticipant.all_objects.filter(event_id=event_uuid, principal_uuid=owner_uuid).exists():
        require_assigned_range_participation(credential, request_id, event_uuid)
