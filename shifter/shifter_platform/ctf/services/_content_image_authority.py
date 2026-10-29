"""Native event policy and exact participation for private first-party images."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctf.exceptions import CTFError, CTFPermissionError
from ctf.services.principal_participation import viewing_participant_for_credential
from ctf.services.unified_authorization import require_event_action
from shared.credentials import CredentialContext, CredentialUnavailable, resolve_current_credential

if TYPE_CHECKING:
    from uuid import UUID

    from ctf.models import CTFContentImage, CTFParticipant


def require_content_image_participant(credential: CredentialContext, event_uuid: UUID) -> CTFParticipant:
    """Admit visible-image discovery with current proof and exact participation."""
    try:
        return viewing_participant_for_credential(credential, event_uuid)
    except CredentialUnavailable:
        raise CTFError("Image authority unavailable", code="CTF_AUTHORITY_UNAVAILABLE") from None
    except ValueError:
        raise CTFPermissionError("Image access denied") from None


def can_read_content_image(credential: CredentialContext, image: CTFContentImage) -> bool:
    """Require current challenge-management policy or exact eligible participation.

    Policy outages never fall back to a participant row. Legacy user, owner and
    staff flags grant no authority, and services never inherit a human's access.
    """
    from ctf.services.content_images import participant_visible_image_urls

    if not isinstance(credential, CredentialContext):
        return False
    try:
        current = resolve_current_credential(credential)
        allowed = _can_manage_images(current, image.event_id)
        if not allowed:
            participant = require_content_image_participant(current, image.event_id)
            allowed = image.source_url in participant_visible_image_urls(participant)
        # Recheck local proof after provider I/O and visibility projection.
        return allowed and resolve_current_credential(current) == current
    except CredentialUnavailable:
        raise CTFError("Image authority unavailable", code="CTF_AUTHORITY_UNAVAILABLE") from None
    except (ValueError, CTFPermissionError):
        return False


def _can_manage_images(credential: CredentialContext, event_uuid: UUID) -> bool:
    """Keep privileged collection access separate from the participant fallback."""
    if credential.kind == "temporary":
        return False
    try:
        require_event_action(credential, event_uuid, "event.manage_challenges")
    except CTFPermissionError:
        return False
    return True
