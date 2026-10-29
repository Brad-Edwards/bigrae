"""Current native admission shared by event notification and scoreboard reads."""

from uuid import UUID

from ctf.exceptions import CTFError, CTFPermissionError
from ctf.services.principal_participation import viewing_participant_for_credential
from ctf.services.unified_authorization import require_event_action
from shared.credentials import CredentialContext, CredentialUnavailable, resolve_current_credential


def authorize_event_projection(credential: CredentialContext, event_uuid: UUID) -> CredentialContext:
    """Require current read policy or independent exact participant read authority."""
    try:
        current = resolve_current_credential(credential)
        if current.kind != "temporary":
            try:
                require_event_action(current, event_uuid, "event.read")
                return current
            except CTFPermissionError:
                pass
        viewing_participant_for_credential(current, event_uuid)
        return current
    except CredentialUnavailable:
        raise CTFError("Event authority unavailable", code="CTF_AUTHORITY_UNAVAILABLE") from None
    except ValueError:
        raise CTFPermissionError("Event projection denied") from None
