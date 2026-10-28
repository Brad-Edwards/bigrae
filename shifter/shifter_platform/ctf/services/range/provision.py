"""Single-participant range provisioning.

Provisions a CTF range for one participant after durably claiming its
assignment, with an exponential-backoff retry wrapper. The keep-alive sleep primitive and the
benign race-loser discriminator live here because both the single-participant
retry and the event-level throttled loop (:mod:`ctf.services.range.batch`)
depend on them.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction

from ctf.exceptions import CTFNotFoundError, CTFRangeError
from ctf.models import CTFEvent, CTFParticipant
from shared.log_sanitize import safe_log_value
from shared.range_instantiation_policy import POLICY_DENIAL_CODE

if TYPE_CHECKING:
    from ctf.models import CTFRangeLaunch
    from ctf.services.actors import EventActionAdmission
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef
    from shared.model_access import OwnedReference

logger = logging.getLogger(__name__)

# Throttled provisioning can sleep for up to 120s between participants and the
# retry backoff can wait even longer. The scheduler's liveness heartbeat file
# is checked at a 1-2 minute staleness threshold, so long waits are broken into
# small chunks that touch the heartbeat between them (and stay responsive to
# shutdown) rather than blocking in a single ``time.sleep`` call.
_HEARTBEAT_CHUNK_SECONDS = 15.0


def _interruptible_sleep(
    seconds: float,
    *,
    heartbeat: Callable[[], None] | None = None,
    shutdown_check: Callable[[], bool] | None = None,
) -> None:
    """Sleep ``seconds`` in <=``_HEARTBEAT_CHUNK_SECONDS`` increments.

    Touches ``heartbeat`` before each increment so a long wait does not let the
    scheduler liveness file go stale, and aborts early when ``shutdown_check``
    returns True so the caller stays responsive to SIGTERM.
    """
    remaining = float(seconds)
    while remaining > 0:
        if shutdown_check is not None and shutdown_check():
            return
        if heartbeat is not None:
            # A heartbeat failure during a wait must never abort provisioning;
            # the per-participant call site logs persistent failures (#942).
            with contextlib.suppress(Exception):
                heartbeat()
        chunk = min(_HEARTBEAT_CHUNK_SECONDS, remaining)
        time.sleep(chunk)
        remaining -= chunk


def _is_already_assigned_error(exc: Exception) -> bool:
    """Return True for the benign 'already has a range' race-loser error (#942).

    A second (manual or scheduled) caller that finds a participant already
    assigned should be skipped, not counted as a provisioning failure.
    """
    return isinstance(exc, CTFRangeError) and "already has a range" in str(exc)


def _underlying_policy_code(exc: Exception) -> str | None:
    """Return the stable policy-denial code from a wrapped error's ``details``.

    CMS carries the ADR-039 classification (identity-or-policy / prerequisite) in
    ``CMSError.details["code"]`` (issue #1348); propagate it onto the CTFRangeError
    so the retry wrapper can recognize a permanent denial.
    """
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        code = details.get("code")
        if isinstance(code, str) and code:
            return code
    return None


def _is_permanent_provisioning_error(exc: Exception) -> bool:
    """Return True for a provisioning error that must not be retried.

    Covers the existing fast-fail validation errors (unregistered user, already
    assigned) and any error carrying the permanent ADR-039 policy-denial code
    (issue #1348): a GDC live-fire denial is permanent, so retrying it only re-runs
    the same denial for every participant with backoff.
    """
    message = str(exc)
    if "must be registered" in message or "already has a range" in message:
        return True
    return getattr(exc, "code", "") == POLICY_DENIAL_CODE


def provision_participant_range(participant_id: UUID, *, actor: object | None = None) -> dict[str, Any]:
    """Provision a range for a participant.

    Uses the event's scenario_id to create a range via CMS.

    Args:
        participant_id: UUID of the participant.

    Returns:
        Dict with range instance ID and initial status.

    Raises:
        CTFNotFoundError: If participant doesn't exist.
        CTFRangeError: If range provisioning fails.
    """
    logger.info("Provisioning range for participant %s", safe_log_value(participant_id))

    if actor is None:
        raise CTFRangeError("Range provisioning authority unavailable")
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import admit_event_actions

    # Resolve the exact initiating credential before claiming capacity or
    # changing participant state.  An authority-free request must be a pure
    # denial, not a participant left behind in ``provisioning``.
    credential = event_actor_credential(actor)
    event_id = CTFParticipant.objects.filter(pk=participant_id).values_list("event_id", flat=True).first()
    if event_id is None:
        raise CTFNotFoundError(
            f"Participant {participant_id} not found",
            details={"participant_id": str(participant_id)},
        )
    event = CTFEvent.objects.get(pk=event_id)
    event_admission = admit_event_actions(credential, event, "event.manage_ranges")
    credential = event_actor_credential(event_admission)
    intent = _claim_for_provisioning(participant_id, event, credential, event_admission)
    from ctf.services.range.launch_intents import execute_launch

    try:
        instance_id = execute_launch(intent)
    except Exception as exc:
        # Intent and capacity remain reserved. An exception cannot establish
        # whether CMS/native creation committed; a retry uses the same UUID.
        raise CTFRangeError(
            f"Range provisioning failed: {exc}",
            code=_underlying_policy_code(exc),
            details={"participant_id": str(participant_id)},
        ) from exc
    return {
        "participant_id": str(participant_id),
        "range_instance_id": instance_id,
        "status": "provisioning",
    }


def _claim_for_provisioning(
    participant_id: UUID, event: CTFEvent, credential: CredentialContext, event_admission: EventActionAdmission
) -> CTFRangeLaunch:
    """Commit capacity, subject claim and immutable request in one transaction."""
    from ctf.services.authorization import assert_event_action
    from ctf.services.model_access_sharing import participant_model_admission_subject
    from ctf.services.range.capacity import admit_range
    from ctf.services.range.launch_intents import launch_parameters, reserve_launch
    from management.services import resolve_principal_uuid

    participant = CTFParticipant.objects.get(pk=participant_id)
    existing, principal_uuid = _participant_launch_snapshot(participant)
    owner = resolve_principal_uuid(principal_uuid)
    subject = participant_model_admission_subject(participant)
    parameters = _participant_launch_parameters(event, participant, owner, subject, existing)
    with transaction.atomic():
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk)
        assert_event_action(event_admission, locked_event, "event.manage_ranges")
        participant = CTFParticipant.objects.select_for_update().get(pk=participant_id)
        _validate_participant_claim(participant, event, owner)
        replay = _locked_participant_replay(participant, existing, credential, parameters)
        if replay is not None:
            return replay
        # A completed old generation may have reached verified terminal cleanup.
        # The next claim gets a fresh immutable intent, never reuses that UUID.
        if launch_parameters(locked_event, owner, subject, None) | {"model_scope": None} != parameters | {
            "model_scope": None
        }:
            raise CTFRangeError("Event changed during range authorization")
        admission = admit_range(event.pk, participant.pk)
        if admission is not None and admission["blocking"]:
            raise CTFRangeError(
                "Range refused: event capacity budget exhausted",
                details={"capacity_reason_codes": admission["reason_codes"]},
            )
        intent = reserve_launch(locked_event, "participant", participant.pk, event_admission, parameters)
        participant.range_request_id = intent.pk
        participant.range_status = "provisioning"
        participant.save(update_fields=["range_request_id", "range_status", "updated_at"])
        return intent


def provision_participant_range_with_retry(
    participant_id: UUID,
    max_retries: int = 3,
    base_delay: int = 30,
    heartbeat: Callable[[], None] | None = None,
    actor: object | None = None,
) -> dict[str, Any]:
    """Provision a range with exponential backoff retry.

    Args:
        participant_id: UUID of the participant.
        max_retries: Maximum retry attempts after initial failure.
        base_delay: Base delay in seconds between retries (doubled each attempt).
        heartbeat: Optional callable invoked during backoff waits so the
            scheduler liveness file stays fresh through long retries.

    Returns:
        Dict with range instance ID, status, and retry count.
    """
    last_error = None

    for attempt in range(1 + max_retries):
        try:
            result = provision_participant_range(participant_id, actor=actor)
            if attempt > 0:
                logger.info(
                    "Provisioning succeeded on attempt %d for participant %s",
                    attempt + 1,
                    participant_id,
                )
            result["retries"] = attempt
            return result
        except CTFRangeError as e:
            # Don't retry permanent errors (validation failures, permanent policy
            # denials) -- retrying only re-runs the same failure with backoff (#1348).
            if _is_permanent_provisioning_error(e):
                raise
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "Provisioning attempt %d failed for participant %s, retrying in %ds: %s",
                    attempt + 1,
                    participant_id,
                    delay,
                    e,
                )
                _interruptible_sleep(delay, heartbeat=heartbeat)

    # All retries exhausted — mark as error
    try:
        participant = CTFParticipant.objects.get(pk=participant_id)
        participant.range_status = "error"
        participant.save(update_fields=["range_status", "updated_at"])
    except CTFParticipant.DoesNotExist:
        pass

    logger.error(
        "Provisioning failed after %d attempts for participant %s: %s",
        1 + max_retries,
        participant_id,
        last_error,
    )
    raise CTFRangeError(
        f"Provisioning failed after {1 + max_retries} attempts: {last_error}",
        details={"participant_id": str(participant_id), "retries": max_retries},
    ) from last_error


def _participant_launch_snapshot(participant: CTFParticipant) -> tuple[CTFRangeLaunch | None, UUID]:
    """Require native registration and distinguish a resumable intent from an active generation."""
    from ctf.models import CTFRangeLaunch

    if participant.user_id is None:
        raise CTFRangeError("Participant must be registered before provisioning a range")
    existing = (
        CTFRangeLaunch.objects.filter(pk=participant.range_request_id).first() if participant.range_request_id else None
    )
    if (existing is None or existing.dispatched_at is not None) and (
        participant.range_instance_id or participant.range_status == "provisioning"
    ):
        raise CTFRangeError("Participant already has a range assigned")
    if participant.principal_uuid is None:
        raise CTFRangeError("Participant must have a native principal before provisioning a range")
    return existing, participant.principal_uuid


def _participant_launch_parameters(
    event: CTFEvent,
    participant: CTFParticipant,
    owner: PrincipalRef,
    subject: OwnedReference,
    existing: CTFRangeLaunch | None,
) -> dict[str, object]:
    """Retained undispatched intent keeps its exact plan; a new generation freezes current facts."""
    from ctf.services.range.launch_intents import launch_parameters
    from ctf.services.range.model_allocation import project_event_model_scope

    if existing is not None and existing.dispatched_at is None:
        return existing.parameters
    return launch_parameters(event, owner, subject, project_event_model_scope(event, participant.pk, subject))


def _validate_participant_claim(participant: CTFParticipant, event: CTFEvent, owner: PrincipalRef) -> None:
    """Recheck the registered subject and event after acquiring the event and participant locks."""
    if participant.event_id != event.pk or participant.principal_uuid != owner.uuid:
        raise CTFRangeError("Participant changed during range authorization")
    if participant.user_id is None:
        raise CTFRangeError("Participant must be registered before provisioning a range")


def _locked_participant_replay(
    participant: CTFParticipant,
    existing: CTFRangeLaunch | None,
    credential: CredentialContext,
    parameters: dict[str, object],
) -> CTFRangeLaunch | None:
    """Resume only an exact locked intent before claiming another participant generation."""
    from ctf.models import CTFRangeLaunch
    from ctf.services.range.launch_intents import assert_launch_replay

    if participant.range_request_id:
        existing = CTFRangeLaunch.objects.select_for_update().filter(pk=participant.range_request_id).first()
    if existing is not None and existing.dispatched_at is None:
        assert_launch_replay(existing, credential, parameters)
        return existing
    if participant.range_instance_id or participant.range_status == "provisioning":
        raise CTFRangeError("Participant already has a range assigned")
    return None
