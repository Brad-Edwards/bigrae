"""CTF Event CRUD: create, read, update, delete, and force-delete.

``_cancel_event_tasks`` is resolved through the ``ctf.services.event``
package at call time (``from ctf.services import event as _e``) rather than
imported directly, so ``unittest.mock.patch`` targets of the form
``patch("ctf.services.event._cancel_event_tasks")`` keep working after the
package split -- see the package ``__init__`` docstring for the full
rationale.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction
from django.db.models import QuerySet

from ctf.enums import EventStatus
from ctf.exceptions import CTFNotFoundError, CTFStateError, CTFValidationError
from ctf.models import CTFEvent
from shared.log_sanitize import safe_log_value

from ._deletion import delete_event, force_delete_event
from ._validation import validate_content_scenario_access, validate_scoring_mode
from ._workspace import resolve_event_placement
from .scheduling import _reschedule_event_tasks, _reschedule_live_event_schedule

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from shared.credentials import CredentialContext

logger = logging.getLogger(__name__)

# Fields that organizers may set when creating or updating events.
# All other fields (status, created_by, id, timestamps, etc.) are
# controlled internally and must not be overwritten by user input.
_EVENT_MUTABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "public_registration_enabled",
        "event_start",
        "event_end",
        "registration_deadline",
        "scenario_id",
        "auto_cleanup",
        "cleanup_delay_hours",
        "max_participants",
        "team_mode",
        "team_size_limit",
        "range_spinup_minutes",
        "range_config",
        "submission_cooldown_seconds",
        "attempt_limit_mode",
        "attempt_limit_cooldown_seconds",
        "rating_visibility",
        "scoreboard_visibility",
        "scoreboard_freeze_at",
        "scoring_mode",
        "rules",
        "reminder_hours",
        "event_timezone",
        "capacity_hints",
        "model_demand",
        "logo_url",
        "visible_os_types",
        "theme_color",
    }
)


def create_event_for_credential(
    credential: CredentialContext,
    event_data: dict[str, Any],
    *,
    creator_user: User | None = None,
    _compatibility_actor: User | None = None,
) -> CTFEvent:
    """Create a new event after exact parent policy, with principal attribution.

    Args:
        credential: The current human or service proof.
        event_data: Dictionary containing event fields.

    Returns:
        The created CTFEvent instance.

    Raises:
        CTFValidationError: If event data is invalid.
    """
    logger.info("Creating CTF event for principal %s", safe_log_value(credential.principal.uuid))
    from ctf.services.actors import event_actor_credential, fence_event_actor
    from shared.credentials import resolve_current_credential

    if _compatibility_actor is None:
        credential = resolve_current_credential(credential)
    elif event_actor_credential(_compatibility_actor) != credential:
        raise CTFValidationError("Event actor changed")

    _validate_creation_fields(event_data)

    validate_scoring_mode(event_data)

    # Filter to allowed fields only — prevent mass assignment of status,
    # created_by, id, timestamps, etc.
    safe_data = {k: v for k, v in event_data.items() if k in _EVENT_MUTABLE_FIELDS}
    scenario_id = str(safe_data.get("scenario_id", CTFEvent._meta.get_field("scenario_id").default))
    placement = resolve_event_placement(credential, event_data, compatibility_actor=_compatibility_actor)
    validate_content_scenario_access(credential, scenario_id, scope=placement.scope)
    from ctf.services.content_resolution import resolve_scenario_ctf_content

    resolved_content = resolve_scenario_ctf_content(scenario_id)
    from .initialization import initialization_intent, resume_event_initialization

    if "model_sources" in event_data:
        from .model_sources import validate_event_model_source_selection

        validate_event_model_source_selection(credential, placement.workspace_id, event_data["model_sources"])
    intent = initialization_intent(credential, event_data, resolved_content)

    with transaction.atomic():
        fence_event_actor(_compatibility_actor if _compatibility_actor is not None else credential)
        from ctf.bridges import cms_fence_pack_reference

        cms_fence_pack_reference(scenario_id)
        event = CTFEvent.objects.create(
            created_by=creator_user,
            creator_principal_uuid=credential.principal.uuid,
            owner_principal_uuid=credential.principal.uuid,
            scope_kind=placement.scope.kind,
            account_id=placement.account_id,
            organization_id=placement.organization_id,
            workspace_id=placement.workspace_id,
            authorization_publication_state="requested",
            initialization_intent=intent,
            status=EventStatus.DRAFT.value,
            **safe_data,
        )
        if event.public_registration_enabled:
            from ctf.services.audit import audit_public_registration_publication

            audit_public_registration_publication(
                actor_id=creator_user.pk if creator_user is not None else None,
                event_id=event.pk,
                enabled=True,
                principal=credential.principal,
            )
    event = resume_event_initialization(
        event.pk,
        resolved_content=resolved_content,
        _admitted_credential=credential,
        _admitted_actor=_compatibility_actor,
    )
    logger.info("Created CTF event %s: %s", event.id, safe_log_value(event.name))
    return event


def create_event(user: User, event_data: dict[str, Any]) -> CTFEvent:
    """Compatibility entrypoint using the same principal policy as native callers."""
    from shared.api.principals import human_session_credential

    return create_event_for_credential(
        human_session_credential(user), event_data, creator_user=user, _compatibility_actor=user
    )


def _validate_event_time_range(event_start: datetime, event_end: datetime) -> None:
    """Raise when event_end is not strictly after event_start."""
    if event_end <= event_start:
        raise CTFValidationError(
            "Event end must be after event start",
            code="CTF_INVALID_DATES",
        )


def _reschedule_event_if_schedule_changed(
    event: CTFEvent,
    safe_data: dict[str, Any],
    *,
    old_event_start: datetime,
    old_event_end: datetime,
    old_cleanup_time: datetime | None,
    actor: object | None,
) -> None:
    """Reschedule pending tasks when event times change."""
    event_end_changed = safe_data.get("event_end", old_event_end) != old_event_end
    schedule_changed = safe_data.get("event_start", old_event_start) != old_event_start or event_end_changed
    if schedule_changed and event.status == EventStatus.REGISTRATION.value:
        if actor is None:
            raise CTFValidationError("Lifecycle authority unavailable")
        _reschedule_event_tasks(event, actor)
    elif event_end_changed and event.status in (
        EventStatus.ACTIVE.value,
        EventStatus.PAUSED.value,
    ):
        _reschedule_live_event_schedule(event)
    if old_cleanup_time is not None and event.get_cleanup_time() != old_cleanup_time:
        from ctf.services._event_range_lease import reconcile_event_range_leases

        reconcile_event_range_leases(event)


_TEAM_CONFIG_FIELDS = frozenset({"team_mode", "team_size_limit"})


def _reject_team_config_changes_after_start(event: CTFEvent, event_data: dict[str, Any]) -> None:
    """CTF-501: team mode and size are structural — frozen once the event starts.

    Changing them mid-competition would strand existing teams or silently
    invalidate the capacity guard, so edits are allowed only in DRAFT and
    REGISTRATION (values equal to the current ones pass through unchanged).
    """
    if event.status in (EventStatus.DRAFT.value, EventStatus.REGISTRATION.value):
        return
    changed = {
        field for field in _TEAM_CONFIG_FIELDS if field in event_data and event_data[field] != getattr(event, field)
    }
    if changed:
        raise CTFStateError(
            "Team settings cannot change after the event starts",
            details={"event_status": event.status, "fields": sorted(changed)},
        )


def get_event(event_id: UUID) -> CTFEvent:
    """Get a CTF event by ID.

    Args:
        event_id: UUID of the event.

    Returns:
        The CTFEvent instance.

    Raises:
        CTFNotFoundError: If event doesn't exist.
    """
    try:
        return CTFEvent.objects.get(pk=event_id)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(
            f"Event {event_id} not found",
            details={"event_id": str(event_id)},
        ) from None


def event_pk_if_exists(event_id: UUID) -> UUID | None:
    """Return the event's primary key if it exists, else ``None``.

    Bounded existence check for cross-domain composition (``config``) that must
    validate a CTF event id without importing the ``ctf`` domain model. Returns a
    primitive, never an ORM object (ADR-001, #1523).
    """
    pk = CTFEvent.objects.filter(pk=event_id).values_list("pk", flat=True).first()
    return pk


def list_events_for_organizer(user: User) -> QuerySet[CTFEvent]:
    """List the CTF events ``user`` may administer (authority-aware).

    Delegates to :func:`ctf.services.event._queries.resolve_administrable_events`
    so this export and ``get_organizer_events`` never become two divergent
    global-access policies (ADR-052-R3).
    """
    from ctf.services.event._queries import resolve_administrable_events

    return resolve_administrable_events(user)


def _validate_creation_fields(event_data: dict[str, Any]) -> None:
    """Require the public creation fields and reject reversed event times."""
    # Validate required fields
    required_fields = ["name", "event_start", "event_end"]
    missing = [f for f in required_fields if f not in event_data]
    if missing:
        raise CTFValidationError(
            f"Missing required fields: {', '.join(missing)}",
            details={"missing_fields": missing},
        )

    # Validate event times
    event_start = event_data.get("event_start")
    event_end = event_data.get("event_end")
    if event_start and event_end and event_end <= event_start:
        raise CTFValidationError(
            "Event end must be after event start",
            code="CTF_INVALID_DATES",
        )


__all__ = [
    "create_event",
    "create_event_for_credential",
    "delete_event",
    "event_pk_if_exists",
    "force_delete_event",
    "get_event",
    "list_events_for_organizer",
]
