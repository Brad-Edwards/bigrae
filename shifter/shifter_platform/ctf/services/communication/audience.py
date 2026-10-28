"""The single audience resolver for scoped communications (ADR-051, #2048).

One closed resolver turns a validated :class:`AudienceKind` selector into a
deterministic, deduplicated, event-qualified set of viewing-eligible
participants. Views, schedulers, and (later) RAES adapters must resolve through
here so audience policy never drifts across surfaces (AC7). Recipient authority
is the event-scoped ``CTFParticipant`` reached through the shared
``viewing_participant_q`` predicate -- never an email address or ambient user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from ctf.communication_contracts import validate_audience_spec
from ctf.enums_communication import AudienceKind
from ctf.exceptions import CTFCommunicationError
from ctf.models import CTFParticipant
from ctf.services.participant.queries import viewing_participant_q

if TYPE_CHECKING:
    from django.db.models import QuerySet


def _as_uuids(values: list[str]) -> list[UUID]:
    """Parse a list of validated UUID strings into ``UUID`` objects."""
    return [UUID(value) for value in values]


def recipient_queryset(target_event_ids: set[UUID], audience_spec: dict[str, Any]) -> QuerySet[CTFParticipant]:
    """Return the unevaluated, event-qualified recipient query for a closed selector.

    Release materializes it; preview aggregates it without loading rows (#2052).
    Every recipient is restricted to ``target_event_ids``; an audience that names
    an event outside the campaign's targets is rejected, and participants/teams
    outside the targets simply do not resolve (no cross-tenant membership oracle).
    """
    spec = validate_audience_spec(audience_spec)
    kind = spec["kind"]
    base = CTFParticipant.objects.filter(viewing_participant_q(), event_id__in=target_event_ids)

    if kind in (AudienceKind.PARTICIPANT.value, AudienceKind.PARTICIPANT_SET.value):
        return base.filter(id__in=_as_uuids(spec["participant_ids"]))
    if kind == AudienceKind.TEAM.value:
        return base.filter(team_id__in=_as_uuids(spec["team_ids"]))
    # EVENT or MULTI_EVENT
    event_ids = _as_uuids(spec["event_ids"])
    if not set(event_ids) <= set(target_event_ids):
        raise CTFCommunicationError(
            "Audience references an event outside the campaign targets",
            code="CTF_COMMUNICATION_AUDIENCE_OUT_OF_SCOPE",
        )
    return base.filter(event_id__in=event_ids)


def resolve_recipients(target_event_ids: set[UUID], audience_spec: dict[str, Any]) -> list[CTFParticipant]:
    """Resolve a closed audience selector to event-qualified recipients.

    Returns viewing-eligible participants ordered by their immutable id so the
    result is deterministic and, because each participant is a single
    event-scoped row, inherently deduplicated.
    """
    qs = recipient_queryset(target_event_ids, audience_spec)
    return list(qs.select_related("event", "team", "user").order_by("id"))
