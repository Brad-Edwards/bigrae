"""Closed staff-role mapping and live native event-policy queries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.contrib.auth.models import User

from ctf.enums import EventCapability, EventStaffRole
from ctf.exceptions import CTFNotFoundError, CTFPermissionError
from ctf.models import CTFEvent, CTFEventStaff

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser

    from ctf.services.actors import EventActor


_EVENT_MANAGE_STAFF = "event.manage_staff"


_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    EventStaffRole.MODERATOR.value: frozenset(
        {EventCapability.PARTICIPANTS.value, EventCapability.NOTIFICATIONS.value}
    ),
    EventStaffRole.JUDGE.value: frozenset({EventCapability.AWARDS.value, EventCapability.SUBMISSIONS.value}),
    EventStaffRole.CO_ORGANIZER.value: frozenset(cap.value for cap in EventCapability),
}


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


ALL_DELEGABLE_CAPABILITIES: tuple[str, ...] = tuple(sorted(set().union(*_ROLE_CAPABILITIES.values())))


def capabilities_for_role(role: str | None) -> tuple[str, ...]:
    """Return the sorted delegable capabilities a staff ``role`` grants (empty if none)."""
    return tuple(sorted(_ROLE_CAPABILITIES.get(role or "", frozenset())))


def actions_for_role(role: str | None) -> tuple[str, ...]:
    """Return the exact OpenFGA application actions granted by a staff role."""
    capabilities = capabilities_for_role(role)
    actions = {_CAPABILITY_ACTIONS[item] for item in capabilities}
    if EventCapability.CONFIG.value in capabilities:
        actions.add("event.read")
    return tuple(sorted(actions))


def capability_action_pairs() -> tuple[tuple[str, str], ...]:
    """Return the closed UI capability-to-policy-action mapping."""
    return tuple(sorted(_CAPABILITY_ACTIONS.items()))


def _live_staff_role(event: CTFEvent, user_id: int) -> str | None:
    """Return the live (non-revoked) staff role for ``user_id`` on ``event``, or None."""
    return (
        CTFEventStaff.objects.filter(event=event, user_id=user_id, deleted_at__isnull=True)
        .values_list("role", flat=True)
        .first()
    )


def actor_is_active_ctf_organizer(user_id: int | None) -> bool:
    """Return whether ``user_id`` is an active account with the global CTF organizer role.

    Staff-derived authority is only valid while the account still holds the
    platform CTF Organizer role: a live ``CTFEventStaff`` row must never be an
    ambient source of authority after the global role is revoked or the account
    is deactivated (#1922 review — stale-row bypass). The owner and platform-admin
    branches do not use this; canonical ownership and the superuser override are
    separate invariants.
    """
    if user_id is None:
        return False
    from ctf.bridges import get_user_role

    user = User.objects.filter(pk=user_id, is_active=True).first()
    return user is not None and get_user_role(user).is_ctf_organizer


def staff_row_grants_capability(actor_pk: int, event: CTFEvent, capability: str) -> bool:
    """Return whether a live staff row for ``actor_pk`` on ``event`` grants ``capability``.

    The raw delegation check only: no owner or platform-admin fallback. The
    authority resolver composes this with owner and override authority so the
    least-authority order lives in one place (ADR-052-R2). A live row grants a
    capability only while the account still holds the global CTF Organizer role
    and is active, so a demoted account keeps no authority through a stale row
    (#1922 review).
    """
    role = _live_staff_role(event, actor_pk)
    if role is None or capability not in _ROLE_CAPABILITIES.get(role, frozenset()):
        return False
    return actor_is_active_ctf_organizer(actor_pk)


def eligible_co_organizer_ids(event: CTFEvent) -> list[int]:
    """Project active human co-organizer candidates; delivery checks native policy."""
    from management.services import active_human_user_ids

    principal_uuids = tuple(
        CTFEventStaff.objects.filter(event=event, role=EventStaffRole.CO_ORGANIZER.value)
        .order_by("principal_uuid")
        .values_list("principal_uuid", flat=True)
    )
    return list(active_human_user_ids(principal_uuids))


def event_notification_organizers(event: CTFEvent) -> dict[UUID, int]:
    """Map current owner and co-organizer principals to accounts without remote policy I/O.

    These are delivery candidates only; every delivery and read checks native policy.
    """
    from management.services import active_human_user_ids_by_principal

    principal_uuids = (
        event.owner_principal_uuid,
        *CTFEventStaff.objects.filter(event=event, role=EventStaffRole.CO_ORGANIZER.value)
        .order_by("principal_uuid")
        .values_list("principal_uuid", flat=True),
    )
    return active_human_user_ids_by_principal(principal_uuids)


def event_notification_organizer_ids(event: CTFEvent) -> list[int]:
    """Map current owner and co-organizer principals without remote policy I/O."""
    return list(event_notification_organizers(event).values())


def actor_has_event_capability(actor: User | AnonymousUser, event: CTFEvent, capability: str) -> bool:
    """Return whether ``actor`` may exercise ``capability`` on ``event``.

    Admits the owning organizer, a live staff row whose role grants the
    capability, or the platform-admin override (ADR-052). Delegates to the
    service-owned authority resolver so callers share one least-authority policy.
    """
    from ctf.services.authorization import resolve_event_authority

    return resolve_event_authority(actor, event, capability=capability) is not None


def _resolve_owned_event_for_staff(event_id: UUID, actor: EventActor, action: str) -> CTFEvent:
    """Resolve the live event and require the exact native policy action.

    Ownership and staff rows describe the event; current policy and the actor's
    credential ceiling authorize this command. Full effect delegation is checked
    separately before retaining a new transition and while settling its effects.
    """
    from ctf.services.authorization import assert_event_action

    try:
        event = CTFEvent.objects.get(pk=event_id, deleted_at__isnull=True)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(f"Event {event_id} not found", details={"event_id": str(event_id)}) from None

    try:
        assert_event_action(actor, event, action)
    except CTFPermissionError as exc:
        raise CTFPermissionError("Event staff authority denied") from exc
    return event
