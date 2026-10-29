"""CTF event deletion and durable range cleanup under the event authority boundary."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction

from ctf.exceptions import CTFNotFoundError, CTFStateError, CTFValidationError
from ctf.models import CTFEvent
from shared.log_sanitize import safe_log_value

if TYPE_CHECKING:
    from shared.credentials import CredentialContext

logger = logging.getLogger(__name__)


def delete_event(event_id: UUID, *, actor_id: object | None = None) -> None:
    """Soft-delete a CTF event.

    Args:
        event_id: UUID of the event to delete.
        actor_id: When supplied, the service asserts the ``delete`` capability (#1922).

    Raises:
        CTFNotFoundError: If event doesn't exist.
    """
    logger.info("Deleting CTF event %s", safe_log_value(event_id))

    from ctf.services import event as _e

    try:
        event = CTFEvent.objects.get(pk=event_id)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(
            f"Event {event_id} not found",
            details={"event_id": str(event_id)},
        ) from None

    if actor_id is not None:
        from ctf.enums import EventCapability
        from ctf.services.authorization import assert_event_capability

        assert_event_capability(actor_id, event, EventCapability.DELETE)

    with transaction.atomic():
        # Cancel any scheduled tasks
        _e._cancel_event_tasks(event)

        # Soft delete
        event.delete(soft=True)

        logger.info("Deleted CTF event %s", safe_log_value(event_id))


def force_delete_event(
    event_id: UUID,
    actor: object,
    confirmation_name: str,
) -> dict[str, Any]:
    """Force-delete a CTF event and all associated resources.

    Permanently removes the event regardless of its current state. All child
    records (challenges, participants, submissions, scores, scheduled tasks,
    etc.) are cascade-deleted. Range instances are destroyed first via the CMS
    bridge.

    Args:
        event_id: UUID of the event to force-delete.
        actor: The user performing the deletion.
        confirmation_name: Must match the event name exactly (case-sensitive).

    Returns:
        Summary dict with event_id, event_name, ranges_destroyed, ranges_failed.

    Raises:
        CTFNotFoundError: If event doesn't exist.
        CTFValidationError: If confirmation_name doesn't match.
    """
    from ctf.enums import EventCapability
    from ctf.models import CTFChallengeFile, CTFParticipant
    from ctf.s3 import delete_challenge_file
    from ctf.services import event as _e
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import assert_event_capability

    # Use all_objects so force delete works on soft-deleted events too
    try:
        event = CTFEvent.all_objects.get(pk=event_id)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(
            f"Event {event_id} not found",
            details={"event_id": str(event_id)},
        ) from None

    # Service-layer authorization (defense in depth, #1922): the owner, a full
    # co-organizer, or the platform-admin override may force-delete; moderators/
    # judges cannot. The view checks this too, but internal callers must not
    # bypass it.
    credential = event_actor_credential(actor)
    assert_event_capability(actor, event, EventCapability.DELETE)

    if confirmation_name != event.name:
        raise CTFValidationError(
            "Confirmation name does not match event name",
            details={
                "expected": event.name,
                "provided": confirmation_name,
            },
        )

    event_name = event.name

    from ctf.services.range.launch_intents import cancel_event_launches

    # The event lock fences concurrent claims and all CMS/native creation.
    # Failed cleanup remains retryable through this same force-delete surface.
    cancelled_intents = cancel_event_launches(event.pk, actor, action="event.delete", delete_event=True)
    event = CTFEvent.all_objects.get(pk=event.pk)

    # Destroy range instances OUTSIDE the atomic block (external HTTP calls).
    # Query participants directly via all_objects to handle soft-deleted events
    # (cleanup_event_ranges uses CTFEvent.objects which skips soft-deleted).
    # These count teardowns *dispatched*, not verified destroyed. Force deletion
    # removes the CTF event/participant owner, but the engine-side request /
    # operation / inventory-verification records (keyed by request_id) are the
    # durable cleanup owner that survives event deletion and drives teardown to
    # verified completion (ADR-063-R4/R5). The ``ranges_destroyed`` result key is a
    # pre-existing API field, so its name is kept; its value is a dispatch count.
    from ctf.services.range.launch_cleanup import event_deletion_targets

    targets = event_deletion_targets(event, actor, cancelled_intents)
    ranges_destroyed, ranges_failed = _queue_range_cleanup(event, credential, targets)
    if ranges_failed:
        raise CTFStateError("Event range cleanup is unavailable", code="CTF_EVENT_CLEANUP_UNAVAILABLE")

    # Delete S3 challenge file blobs before cascade removes the DB rows
    s3_keys = list(
        CTFChallengeFile.all_objects.filter(
            challenge__event=event,
        ).values_list("s3_key", flat=True)
    )
    for s3_key in s3_keys:
        try:
            delete_challenge_file(s3_key)
        except Exception:
            logger.exception("Failed to delete S3 object %s during force delete", s3_key)

    # Hard delete inside atomic block — Django CASCADE handles children. The
    # cascade removes the participant rows the retention purge discovers accounts
    # through, so release each temporary account (anonymize, freeing its username)
    # and fence its communications first, committed with the delete itself.
    from ctf.services.communication import on_participant_removed
    from ctf.services.participant.accounts import anonymize_participant_account

    with transaction.atomic():
        _e._cancel_event_tasks(event)
        for participant in CTFParticipant.all_objects.filter(event=event, user__isnull=False):
            if anonymize_participant_account(participant.pk):
                on_participant_removed(participant)
        event.delete(soft=False)

    logger.warning(
        "FORCE DELETE: Event %s (%s) permanently deleted by an authorized principal. "
        "Ranges destroyed: %d, ranges failed: %d.",
        safe_log_value(event_id),
        safe_log_value(event_name),
        ranges_destroyed,
        ranges_failed,
    )

    return {
        "event_id": str(event_id),
        "event_name": event_name,
        "ranges_destroyed": ranges_destroyed,
        "ranges_failed": ranges_failed,
    }


def _queue_range_cleanup(
    event: CTFEvent,
    credential: CredentialContext,
    targets: Sequence[tuple[int | None, UUID | None]],
) -> tuple[int, int]:
    """Dispatch each retained cleanup target and retain failures for the next retry."""
    from ctf.bridges import cms_destroy_event_range
    from ctf.models import CTFParticipant
    from ctf.services.credential_scope import _retained_event_cleanup_scope

    ranges_destroyed = 0
    ranges_failed = 0
    participants_with_ranges = CTFParticipant.all_objects.filter(
        event=event,
        range_instance_id__isnull=False,
    )
    for range_id, request_id in targets:
        try:
            with _retained_event_cleanup_scope(event.pk):
                if request_id is None:
                    cms_destroy_event_range(credential, event.pk, range_id)
                else:
                    cms_destroy_event_range(credential, event.pk, None, request_id=request_id)
            if range_id is not None:
                participants_with_ranges.filter(range_instance_id=range_id).update(range_status="destroying")
            ranges_destroyed += 1
        except Exception:
            ranges_failed += 1
            logger.exception(
                "Failed to queue range cleanup %s during event deletion",
                range_id or request_id,
            )

    return ranges_destroyed, ranges_failed
