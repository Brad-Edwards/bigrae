"""Shared helpers for the canonical CTF organizer views.

Constants, the actor accessor, the ownership/resolution helpers, the raise-based
error helpers, and the organizer detail payload builders shared across the
``ctf.api.organizer`` view modules.

The resolution / ownership / validation helpers RAISE
:class:`ctf.api._base._CtfApiError` instead of returning ``(obj, error)`` tuples,
so each view method wraps its body in a single ``except _CtfApiError`` that
renders the exact legacy status code and message via
:func:`shared.api.errors.api_error_response`. Service and bridge calls are
imported lazily inside the helpers so the existing ``patch("ctf.services...")`` /
``patch("ctf.bridges...")`` test seams continue to intercept them at call time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import status
from rest_framework.request import Request

from ctf.api._base import _CtfApiError, ctf_actor_user

# The override-audit helpers live in ``_audit`` (file-size budget, ADR-052);
# ``_delete_via_service`` uses this one to audit database-only nested deletes.
from ctf.api.organizer._audit import _audit_admin_from_request
from ctf.enums import EventCapability
from shared.api.principals import authenticated_credential
from shared.api_tokens import scopes
from shared.credentials import CredentialContext

# Staff-delegable capability selector: one noun, several, or None (owner-only).
Capability = str | tuple[str, ...] | None

_CAPABILITY_ACTIONS = {
    EventCapability.CONFIG.value: "event.manage_config",
    EventCapability.CHALLENGES.value: "event.manage_challenges",
    EventCapability.PARTICIPANTS.value: "event.manage_participants",
    EventCapability.TEAMS.value: "event.manage_teams",
    EventCapability.RANGES.value: "event.manage_ranges",
    EventCapability.SCORING.value: "event.manage_scoring",
    EventCapability.NOTIFICATIONS.value: "event.manage_communications",
    EventCapability.AWARDS.value: "event.manage_awards",
    EventCapability.SUBMISSIONS.value: "event.manage_submissions",
    EventCapability.CONTENT.value: "event.manage_content",
    EventCapability.LIFECYCLE.value: "event.manage_lifecycle",
    EventCapability.DELETE.value: "event.delete",
}

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any, NoReturn
    from uuid import UUID

    from django.contrib.auth.models import User
    from rest_framework.response import Response

    from ctf.models import CTFChallenge, CTFEvent, CTFParticipant
    from ctf.services.authorization import EventAuthoritySource

_EVENT_READ = (scopes.CTF_EVENT_READ,)
_EVENT_WRITE = (scopes.CTF_EVENT_WRITE,)
_PLAY_READ = (scopes.CTF_PLAY_READ,)
_PLAY_WRITE = (scopes.CTF_PLAY_WRITE,)
_EVENT_OR_PLAY_READ = (scopes.CTF_EVENT_READ, scopes.CTF_PLAY_READ)
_INVALID_EVENT = "Invalid event request."
_EVENT_NOT_FOUND = "Event not found"
_FORBIDDEN = "Forbidden"
_CHALLENGE_NOT_FOUND = "Challenge not found"
_INVALID_CHALLENGE = "Invalid challenge request."
_CHALLENGE_ACTION_FAILED = "Could not process challenge action."
_CHALLENGE_OR_PARTICIPANT_NOT_FOUND = "Challenge or participant not found."
_NO_MORE_HINTS = "No more hints available"
_PARTICIPANT_NOT_FOUND = "Participant not found"
_INVALID_PARTICIPANT_REQUEST = "Invalid participant request."
_BRACKET_NOT_FOUND = "Bracket not found"
_RANGE_REQUEST_FAILED = "Could not process range request."
_RECOVERY_REQUEST_FAILED = "Could not process range recovery request."
_SPARE_POOL_REQUEST_FAILED = "Could not process spare pool request."
_NOTIFICATION_NOT_FOUND = "Notification not found"
_INVALID_NOTIFICATION = "Invalid notification request."
# Sane operator-facing upper bound on a single spare-pool top-up request: large
# enough for any real event's recovery pool, small enough to block a
# fat-fingered or malicious request from queuing unbounded provisioning work.
_MAX_SPARE_POOL_COUNT = 25


def _actor(request: Request) -> User:
    """Return the organizer actor (guaranteed non-None after permission checks)."""
    actor = ctf_actor_user(request)
    if actor is None:
        # Permission classes already admitted the request, so this is defensive.
        raise AssertionError("CTF organizer actor unavailable after permission check")
    return actor


def _credential(request: Request) -> CredentialContext:
    """Return the canonical human or service credential for service calls."""
    try:
        return authenticated_credential(request)
    except ValueError:
        raise AssertionError("CTF credential unavailable after permission check") from None


def _raise_invalid_event() -> NoReturn:
    """Raise the shared 400 envelope for an invalid event request."""
    raise _CtfApiError(code="invalid", message=_INVALID_EVENT, status_code=status.HTTP_400_BAD_REQUEST)


def _raise_forbidden() -> NoReturn:
    """Raise the shared 403 envelope with the legacy ``Forbidden`` message."""
    raise _CtfApiError(code="permission_denied", message=_FORBIDDEN, status_code=status.HTTP_403_FORBIDDEN)


def _raise_not_found(message: str) -> NoReturn:
    """Raise the shared 404 envelope carrying a controlled message."""
    raise _CtfApiError(code="not_found", message=message, status_code=status.HTTP_404_NOT_FOUND)


def _raise_bad_request(message: str) -> NoReturn:
    """Raise the shared 400 envelope carrying a controlled message."""
    raise _CtfApiError(code="invalid", message=message, status_code=status.HTTP_400_BAD_REQUEST)


def _raise_conflict(message: str) -> NoReturn:
    """Raise the shared 409 envelope carrying a controlled message."""
    raise _CtfApiError(code="conflict", message=message, status_code=status.HTTP_409_CONFLICT)


def _raise_throttled(message: str) -> NoReturn:
    """Raise the shared 429 envelope carrying a controlled message."""
    raise _CtfApiError(code="throttled", message=message, status_code=status.HTTP_429_TOO_MANY_REQUESTS)


def _resolve_owned_event(
    request: Request,
    event_id: UUID,
    *,
    capability: Capability = None,
    action: str | None = None,
) -> CTFEvent:
    """Resolve an event and enforce ownership, or raise ``_CtfApiError``.

    Mirrors ``ctf.views.api._common._resolve_owned_event_json``: 404 when the
    event does not exist, 403 when the actor does not own it. When
    ``capability`` is given, delegated event staff whose role grants that
    capability are admitted too (CTF-607); config surfaces pass no
    capability and stay organizer-only.
    """
    from ctf.exceptions import CTFNotFoundError
    from ctf.services import get_event

    try:
        event = get_event(event_id)
    except CTFNotFoundError:
        _raise_not_found(_EVENT_NOT_FOUND)
    if not _actor_may_manage(request, event, capability, action=action):
        _raise_forbidden()
    return event


def _event_authority() -> EventAuthoritySource | None:
    """Return the sole application-policy authority source."""
    from ctf.services.authorization import EventAuthoritySource

    return EventAuthoritySource.POLICY


def _capture_event_authority(request: Request, event: CTFEvent, source: EventAuthoritySource | None) -> None:
    """Stash the resolved (event, authority source) on the request for later audit.

    Every organizer resolver records the authority it admitted so any subsequent
    mutation on this request can write the mandatory platform-admin override
    audit without re-resolving (ADR-052-R4). The last resolved event wins, which
    is correct because a request mutates exactly one event-derived resource.
    """
    request._ctf_admin_authority = (event, source)


def _actor_may_manage(
    request: Request,
    event: CTFEvent,
    capability: Capability,
    *,
    action: str | None = None,
    retained_delete: bool = False,
) -> bool:
    """Require the shared principal decision and capture only presentation provenance."""
    from ctf.exceptions import CTFError, CTFPermissionError

    actions = (action,) if action is not None else _actions_for_capability(capability)
    try:
        credential = authenticated_credential(request)
        if retained_delete:
            from ctf.services.unified_authorization import require_retained_event_delete

            require_retained_event_delete(credential, event.pk)
        elif not any(_event_action_allowed(credential, event.pk, item) for item in actions):
            return False
    except CTFPermissionError:
        return False
    except CTFError as exc:
        raise _CtfApiError(
            code="dependency_unavailable",
            message="Service unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    source = _event_authority()
    _capture_event_authority(request, event, source)
    return True


def _event_action_allowed(credential: CredentialContext, event_id: UUID, action: str) -> bool:
    """Evaluate one exact event action for the current native credential."""
    from ctf.exceptions import CTFError, CTFPermissionError
    from ctf.services.unified_authorization import require_event_action

    try:
        require_event_action(credential, event_id, action)
    except CTFPermissionError:
        return False
    except CTFError as exc:
        raise _CtfApiError(
            code="dependency_unavailable",
            message="Service unavailable",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return True


def _actions_for_capability(capability: Capability) -> tuple[str, ...]:
    """Map a retained UI capability to its registered event actions."""
    actions: tuple[str, ...] = ("event.manage_staff",)
    if capability is not None:
        values = (capability,) if isinstance(capability, str) else capability
        try:
            actions = tuple(_CAPABILITY_ACTIONS[str(value)] for value in values)
        except KeyError:
            actions = ()
    return actions


def _resolve_owned_challenge(request: Request, challenge_id: UUID) -> CTFChallenge:
    """Resolve a challenge and enforce event ownership, or raise ``_CtfApiError``.

    Mirrors ``ctf.views.api._common._resolve_owned_challenge_json``: 404 when the
    challenge does not exist, 403 when the actor does not own its event.
    """
    from ctf.exceptions import CTFNotFoundError
    from ctf.services import get_challenge

    try:
        challenge = get_challenge(challenge_id)
    except CTFNotFoundError:
        _raise_not_found(_CHALLENGE_NOT_FOUND)
    if not _actor_may_manage(request, challenge.event, EventCapability.CHALLENGES):
        _raise_forbidden()
    return challenge


def _resolve_owned_participant(
    request: Request, participant_id: UUID, *, capability: Capability = None
) -> CTFParticipant:
    """Resolve a participant and enforce event ownership, or raise ``_CtfApiError``.

    Mirrors ``ctf.views._access._resolve_owned_participant``: 404 when the
    participant does not exist, 403 when the actor does not own its event.
    ``capability`` admits delegated staff exactly as in
    :func:`_resolve_owned_event`.
    """
    from ctf.exceptions import CTFNotFoundError
    from ctf.services import get_participant

    try:
        participant = get_participant(participant_id)
    except CTFNotFoundError:
        _raise_not_found(_PARTICIPANT_NOT_FOUND)
    if not _actor_may_manage(request, participant.event, capability):
        _raise_forbidden()
    return participant


def _resolve_challenge_participant(request: Request, challenge_id: UUID) -> CTFParticipant:
    """Resolve a challenge (404) then the actor's participant scoped to its event (403).

    Mirrors ``ctf.views.api.play._resolve_challenge_participant``.
    """
    from ctf.exceptions import CTFNotFoundError
    from ctf.services.challenge import get_challenge
    from ctf.services.participant import get_participant_by_user

    try:
        challenge = get_challenge(challenge_id)
    except CTFNotFoundError:
        _raise_not_found(_CHALLENGE_NOT_FOUND)
    participant = get_participant_by_user(_actor(request), event_id=challenge.event_id)
    if not participant:
        _raise_forbidden()
    return participant


def _resolve_active_participant(request: Request) -> CTFParticipant | None:
    """Resolve the participant for the actor's active event, or ``None``.

    Mirrors ``ctf.views._access._get_active_participant``: the participant is
    scoped to ``get_user_role(actor).active_ctf_event`` rather than an unscoped
    first-row pick, so a user enrolled in several events acts as the right one.
    """
    from ctf.bridges import get_user_role
    from ctf.services.participant import get_participant_by_user

    actor = ctf_actor_user(request)
    if actor is None:
        return None
    role = get_user_role(actor)
    if role.active_ctf_event is None:
        return None
    return get_participant_by_user(actor, event_id=role.active_ctf_event.id)


def _delete_via_service(
    request: Request, action_fn: Callable[..., Any], target_id: UUID, *, operation: str | None = None
) -> Response:
    """Run a delete-style service action, returning ``{"success": True}`` or raising an error.

    Mirrors ``ctf.views.api._common._delete_via_service_response``:
    ``CTFPermissionError`` -> 403, ``CTFNotFoundError`` -> 404,
    ``CTFStateError`` -> 400, each raised as ``_CtfApiError`` for the caller's
    ``except`` to render.

    When ``operation`` is given this is the single point that audits an
    event-derived delete performed with the platform-admin override: the
    database-only delete and its strict override audit share one transaction, so
    a strict audit failure rolls the delete back (ADR-052-R4). The audit is a
    no-op for owner or delegated-staff authority.
    """
    from django.db import transaction
    from rest_framework.response import Response

    from ctf.exceptions import CTFNotFoundError, CTFPermissionError, CTFStateError
    from shared.audit import AuditAction

    try:
        with transaction.atomic():
            action_fn(target_id, actor_id=_credential(request))
            if operation is not None:
                _audit_admin_from_request(request, operation, action=AuditAction.DELETE)
    except CTFPermissionError:
        _raise_forbidden()
    except CTFNotFoundError:
        _raise_not_found("Resource not found.")
    except CTFStateError:
        _raise_bad_request("Invalid request.")
    return Response({"success": True})


def _challenge_detail_payload(challenge: CTFChallenge) -> dict[str, object]:
    """Render the organizer GET-challenge JSON payload.

    Mirrors ``ctf.views.api.challenges._challenge_detail_payload`` key-for-key.
    """
    return {
        "id": str(challenge.id),
        "name": challenge.name,
        "description": challenge.description,
        "category": challenge.category,
        "points": challenge.points,
        "difficulty": challenge.difficulty,
        "flag_format": challenge.flag_format,
        "hints": [
            {"id": str(h.id), "text": h.text, "penalty": h.penalty, "order": h.order} for h in challenge.hints.all()
        ],
        "max_attempts": challenge.max_attempts,
        "minimum_points": challenge.minimum_points,
        "decay_function": challenge.decay_function,
        "decay_solve_count": challenge.decay_solve_count,
        "order": challenge.order,
        "release_time": challenge.release_time.isoformat() if challenge.release_time else None,
        "visibility": challenge.visibility,
        "target_instance_name": challenge.target_instance_name,
        "target_port": challenge.target_port,
        "tags": list(challenge.tags.values_list("name", flat=True)),
        "topics": list(challenge.topics.values_list("name", flat=True)),
        "solution": challenge.solution,
        "rating": _organizer_rating(challenge),
    }


def _organizer_rating(challenge: CTFChallenge) -> dict[str, float | int | None] | None:
    """Aggregate challenge rating for the organizer view (CTF-120).

    Organizers see the aggregate for both ``public`` and ``organizer``
    visibility; ``None`` only when ratings are disabled for the event.
    """
    if challenge.event.rating_visibility == "disabled":
        return None
    from ctf.services.submission import get_challenge_rating

    return get_challenge_rating(challenge.id)


def _pagination_window(request: Request, *, max_limit: int = 500) -> tuple[int, int | None]:
    """Parse optional ``offset``/``limit`` query params for list endpoints (CTF-1201).

    Returns ``(offset, limit)``; limit is None when the caller did not ask to
    paginate, preserving the historical full-list responses.
    """
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    raw_limit = request.query_params.get("limit")
    if raw_limit is None:
        return offset, None
    try:
        limit = min(max(1, int(raw_limit)), max_limit)
    except (TypeError, ValueError):
        return offset, None
    return offset, limit


def _participant_username(participant: CTFParticipant) -> str | None:
    """Return the isolated CTF account's login handle, or None.

    Only isolated (#1206) accounts expose their username to organizers; a
    linked platform account's handle is not CTF-surface data. Guarded because
    ``user.profile`` raises (not returns None) when the profile row is absent.
    """
    from django.core.exceptions import ObjectDoesNotExist

    user = participant.user
    if user is None:
        return None
    try:
        is_ctf_account = user.profile.is_ctf_account
    except ObjectDoesNotExist:
        is_ctf_account = False
    return user.username if is_ctf_account else None


def _participant_detail_payload(participant: CTFParticipant) -> dict[str, object]:
    """Render the organizer GET-participant JSON payload.

    Mirrors ``ctf.views.api.participants._participant_detail_payload`` key-for-key.
    """
    from ctf.models import CTFSubmission

    submissions = CTFSubmission.objects.filter(participant=participant)
    correct_submissions = submissions.filter(is_correct=True)
    return {
        "id": str(participant.id),
        "name": participant.name,
        "email": participant.email,
        "status": participant.status,
        "status_reason": participant.status_reason,
        "role": participant.role,
        "hidden": participant.hidden,
        "affiliation": participant.affiliation,
        "username": _participant_username(participant),
        "team_name": participant.team.name if participant.team else None,
        "registered_at": participant.registered_at.isoformat() if participant.registered_at else None,
        "login_info_sent_at": participant.login_info_sent_at.isoformat() if participant.login_info_sent_at else None,
        "last_active_at": participant.last_active_at.isoformat() if participant.last_active_at else None,
        "total_score": participant.total_score,
        "solved_count": correct_submissions.count(),
        "attempt_count": submissions.count(),
        "event_id": str(participant.event_id),
        "bracket_id": str(participant.bracket_id) if participant.bracket_id else None,
        "bracket_name": participant.bracket.name if participant.bracket else None,
        # CTF-204: organizer-granted bonuses/deductions in the score breakdown.
        "awards": [
            {
                "id": str(award.id),
                "points": award.points,
                "reason": award.reason,
                "granted_by": award.granted_by.get_username() if award.granted_by else None,
                "created_at": award.created_at.isoformat() if award.created_at else None,
            }
            for award in participant.awards.all()
        ],
    }
