"""Idempotent, checkpointed steps of participant range recovery (issue #1018).

Split out of :mod:`ctf.services.range.recovery` (Sonar S104). Each step is
resume-safe: it derives "already done" from the recorded recovery *data*, so a
retry after a crash never repeats a completed side effect. Orchestration,
claiming, and failure classification stay in the parent module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone

from ctf.enums import RecoveryFailureCategory, RecoveryPhase, SpareRangeStatus
from ctf.exceptions import CTFRangeError
from ctf.models import CTFParticipant, CTFRangeRecovery, CTFSpareRange
from ctf.services.audit import audit_range_recovery
from shared.enums import ResourceStatus

if TYPE_CHECKING:
    from ctf.models import CTFEvent, CTFRangeLaunch
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope

if TYPE_CHECKING:
    from uuid import UUID

    from shared.model_access import OwnedReference

logger = logging.getLogger(__name__)

# Range statuses that mean "already torn down" -- teardown is a no-op for these.
_OLD_RANGE_TERMINAL_STATUSES = {ResourceStatus.DESTROYED.value, ResourceStatus.FAILED.value}


def _range_error(message: str, *, category: RecoveryFailureCategory, **details: Any) -> CTFRangeError:
    """Build a `CTFRangeError` carrying an authored failure category for classification."""
    details = {**details, "failure_category": category.value}
    return CTFRangeError(message, details=details)


def _participant_principal_uuid(participant: CTFParticipant) -> UUID:
    """Require the participant identity that owns the replacement range."""
    principal_uuid = participant.principal_uuid
    if principal_uuid is None:
        raise _range_error("Participant principal unavailable", category=RecoveryFailureCategory.VALIDATION_FAILED)
    return principal_uuid


def _rebuild_replacement(
    recovery: CTFRangeRecovery,
    participant: CTFParticipant,
    model_subject: OwnedReference,
    credential: CredentialContext,
) -> tuple[int, UUID]:
    """Reserve and resume exactly one replacement UUID before invoking CMS."""
    from ctf.models import CTFEvent, CTFRangeLaunch
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import admit_event_actions, assert_event_action
    from ctf.services.range.launch_intents import (
        assert_launch_replay,
        execute_launch,
        launch_parameters,
        reserve_launch,
    )
    from ctf.services.range.model_allocation import project_event_model_scope
    from management.services import resolve_principal_uuid

    event = participant.event
    event_admission = admit_event_actions(credential, event, "event.manage_ranges")
    credential = event_actor_credential(event_admission)
    owner = resolve_principal_uuid(_participant_principal_uuid(participant))
    existing = (
        CTFRangeLaunch.objects.filter(pk=recovery.replacement_request_id).first()
        if recovery.replacement_request_id is not None
        else None
    )
    parameters = (
        existing.parameters
        if existing
        else launch_parameters(
            event, owner, model_subject, project_event_model_scope(event, participant.pk, model_subject)
        )
    )
    with transaction.atomic():
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk)
        assert_event_action(event_admission, locked_event, "event.manage_ranges")
        locked = CTFRangeRecovery.objects.select_for_update().get(pk=recovery.pk)
        intent = (
            CTFRangeLaunch.objects.filter(pk=locked.replacement_request_id).first()
            if locked.replacement_request_id is not None
            else None
        )
        if intent is None:
            intent = reserve_launch(locked_event, "rebuild", locked.pk, event_admission, parameters)
            locked.replacement_request_id = intent.pk
            locked.save(update_fields=["replacement_request_id", "updated_at"])
        else:
            assert_launch_replay(intent, credential, parameters)
        recovery.replacement_request_id = intent.pk
    try:
        return execute_launch(intent), intent.pk
    except Exception as exc:
        # Range failures can carry private provider diagnostics.
        logger.error(
            "Replacement range provisioning failed participant_id=%s exc_type=%s",
            participant.pk,
            type(exc).__name__,
            exc_info=False,
        )
        raise _range_error(
            "Replacement range provisioning failed",
            category=RecoveryFailureCategory.PROVISIONING_FAILED,
            participant_id=str(participant.pk),
        ) from exc


def _claim_spare(participant: CTFParticipant, spare_range_instance_id: int | None) -> CTFSpareRange | None:
    """Durably reserve a compatible spare from the participant's event pool, or `None`.

    Selects a READY, unconsumed, event-scoped spare ``FOR UPDATE`` and marks it
    CONSUMED so a concurrent recovery cannot take it -- a query-only check would
    race (a competitor could consume it between check and attach, after this
    recovery destroyed the old range, stranding the participant, #307 review).
    Event scoping (the ``CTFSpareRange.event`` FK) is the tenant boundary even
    for an explicitly named spare; a stale-local-status spare is confirmed
    live-READY via the bridge first. Ownership moves later, in
    :func:`_ensure_spare_attached` (after teardown), per the #307 constraint.
    MUST run inside the caller's transaction (``select_for_update``);
    :func:`_ensure_spare_reserved` wraps this claim and the pointer write atomically.
    """
    from ctf.models import CTFEvent

    event = CTFEvent.objects.select_for_update().get(pk=participant.event_id)
    if event.status == "cancelled":
        return None
    from workspaces.services import resource_scope_from_ids

    event_scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    candidates = (
        CTFSpareRange.objects.select_for_update()
        .filter(event=event, consumed_by__isnull=True)
        .exclude(status=SpareRangeStatus.FAILED.value)
    )
    if spare_range_instance_id is not None:
        candidates = candidates.filter(range_instance_id=spare_range_instance_id)

    for candidate in candidates.order_by("-created_at"):
        if not _spare_generation_eligible(candidate, event, event_scope):
            continue
        candidate.consumed_by = participant
        candidate.consumed_at = timezone.now()
        candidate.status = SpareRangeStatus.CONSUMED.value
        candidate.save(update_fields=["consumed_by", "consumed_at", "status", "updated_at"])
        return candidate
    return None


def _ensure_spare_reserved(
    recovery: CTFRangeRecovery, participant: CTFParticipant, spare_range_instance_id: int | None
) -> None:
    """Reserve the replacement spare before any teardown (idempotent).

    Records the claimed spare id (a resume reuses it). Raising (no spare) happens
    BEFORE teardown, so a missing spare never strands the participant (#1018).
    """
    from ctf.models import CTFEvent, CTFRangeLaunch

    # Claim + record the pointer in ONE transaction: a crash between them rolls
    # both back, so a spare is never CONSUMED without a durable recovery pointer.
    # Event cancellation and a second recovery attempt take the same mutexes.
    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().get(pk=participant.event_id)
        if event.status == "cancelled":
            raise _range_error("Event is cancelled", category=RecoveryFailureCategory.NO_COMPATIBLE_SPARE)
        locked = CTFRangeRecovery.objects.select_for_update().get(pk=recovery.pk)
        recovery.replacement_range_instance_id = locked.replacement_range_instance_id
        recovery.replacement_request_id = locked.replacement_request_id
        if locked.replacement_range_instance_id is not None:
            from ctf.bridges import cms_range_egress_compatible_with_event
            from workspaces.services import resource_scope_from_ids

            spare = (
                CTFSpareRange.objects.select_for_update()
                .filter(
                    event=event,
                    consumed_by=participant,
                    range_instance_id=locked.replacement_range_instance_id,
                    request_id=locked.replacement_request_id,
                    status=SpareRangeStatus.CONSUMED.value,
                )
                .first()
            )
            live = (
                spare is not None
                and spare.request_id is not None
                and CTFRangeLaunch.objects.filter(
                    pk=spare.request_id,
                    event=event,
                    subject_kind="spare",
                    subject_uuid=spare.pk,
                    range_instance_id=spare.range_instance_id,
                ).exists()
            )
            event_scope = resource_scope_from_ids(
                kind=event.scope_kind,
                account_id=event.account_id,
                organization_id=event.organization_id,
                workspace_id=event.workspace_id,
            )
            if not live or not cms_range_egress_compatible_with_event(
                locked.replacement_range_instance_id, event_scope
            ):
                raise _range_error(
                    "No compatible spare range available for reassignment",
                    category=RecoveryFailureCategory.NO_COMPATIBLE_SPARE,
                    participant_id=str(participant.pk),
                    event_id=str(event.pk),
                )
            return
        spare = _claim_spare(participant, spare_range_instance_id)
        if spare is None:
            raise _range_error(
                "No compatible spare range available for reassignment",
                category=RecoveryFailureCategory.NO_COMPATIBLE_SPARE,
                participant_id=str(participant.pk),
                event_id=str(participant.event_id),
            )
        locked.replacement_range_instance_id = recovery.replacement_range_instance_id = spare.range_instance_id
        locked.replacement_request_id = recovery.replacement_request_id = spare.request_id
        locked.save(update_fields=["replacement_range_instance_id", "replacement_request_id", "updated_at"])


def _ensure_spare_attached(
    recovery: CTFRangeRecovery, participant: CTFParticipant, credential: CredentialContext
) -> None:
    """Attach the reserved spare after the old range is blocked (idempotent, #307).

    ``cms_reassign_range_owner`` is a no-op when already owned (resume safe).
    """
    from ctf.bridges import cms_reassign_range_owner
    from management.services import resolve_principal_uuid

    replacement_id = recovery.replacement_range_instance_id
    if replacement_id is None:
        raise _range_error(
            "Reserved replacement range id missing during attach",
            category=RecoveryFailureCategory.INTERNAL_ERROR,
            recovery_id=str(recovery.pk),
        )

    cms_reassign_range_owner(
        credential,
        replacement_id,
        resolve_principal_uuid(_participant_principal_uuid(participant)),
        participant.event_id,
    )

    recovery.phase = RecoveryPhase.REPLACEMENT_READY.value
    recovery.save(update_fields=["phase", "updated_at"])


def _ensure_rebuild_replacement_ready(
    recovery: CTFRangeRecovery,
    participant: CTFParticipant,
    model_subject: OwnedReference,
    credential: CredentialContext,
) -> None:
    """Provision a fresh rebuild replacement range (idempotent; rebuild-only).

    reassign_spare reserves via :func:`_ensure_spare_reserved` before teardown.
    ``model_subject`` is the pre-teardown authoritative membership subject (PLAT-202).
    """
    replacement_id, request_id = _rebuild_replacement(recovery, participant, model_subject, credential)
    recovery.replacement_range_instance_id = replacement_id
    recovery.replacement_request_id = request_id
    recovery.phase = RecoveryPhase.REPLACEMENT_READY.value
    recovery.save(update_fields=["replacement_range_instance_id", "replacement_request_id", "phase", "updated_at"])


def _ensure_old_range_blocked(
    recovery: CTFRangeRecovery, old_range_instance_id: int, credential: CredentialContext
) -> None:
    """Tear down the old range unless it is already in a terminal state.

    Idempotent against retries: ``cms.services.destroy_range`` is not itself
    safely re-callable on an already-soft-deleted range (it raises "not
    found"), so the live status is checked first via
    ``ctf.bridges.cms_get_range_status`` before dispatching teardown.
    """
    from ctf.bridges import cms_destroy_recovery_range, cms_get_range_status

    current_status = cms_get_range_status(old_range_instance_id)
    if current_status not in _OLD_RANGE_TERMINAL_STATUSES:
        try:
            cms_destroy_recovery_range(credential, recovery.event_id, old_range_instance_id)
        except Exception as e:
            # Authored message only: this log line and the chained cause carry the detail.
            logger.exception("Failed to block old range %s", old_range_instance_id)
            raise _range_error(
                f"Failed to block old range {old_range_instance_id}",
                category=RecoveryFailureCategory.OLD_RANGE_TEARDOWN_FAILED,
                old_range_instance_id=old_range_instance_id,
            ) from e

    recovery.phase = RecoveryPhase.OLD_RANGE_BLOCKED.value
    recovery.save(update_fields=["phase", "updated_at"])


def _ensure_participant_repointed(participant: CTFParticipant, recovery: CTFRangeRecovery) -> None:
    """Bind the exact live replacement while cancellation holds the same event mutex."""
    from ctf.bridges import cms_get_range_status
    from ctf.models import CTFEvent, CTFRangeLaunch

    replacement_id = recovery.replacement_range_instance_id
    replacement_request = recovery.replacement_request_id
    if replacement_id is None or replacement_request is None:
        # The replacement id is recorded first (reserve / rebuild-ready step).
        raise _range_error(
            "Replacement range id missing during repoint",
            category=RecoveryFailureCategory.INTERNAL_ERROR,
            recovery_id=str(recovery.pk),
        )

    expected_old = (recovery.old_range_instance_id, participant.range_request_id)
    from ctf.bridges import cms_is_event_range_cleanup_verified_absent

    cleared_old = participant.range_request_id is not None and cms_is_event_range_cleanup_verified_absent(
        participant.event_id, recovery.old_range_instance_id, participant.range_request_id
    )
    replacement = (replacement_id, replacement_request)
    replacement_status = cms_get_range_status(replacement_id)
    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().get(pk=participant.event_id)
        locked = CTFRangeRecovery.objects.select_for_update().get(pk=recovery.pk, event=event, participant=participant)
        current = CTFParticipant.objects.select_for_update().get(pk=participant.pk, event=event)
        intent = CTFRangeLaunch.objects.filter(
            pk=replacement_request, event=event, range_instance_id=replacement_id
        ).first()
        live_subject = _live_replacement_subject(intent, locked, current, replacement)
        if (
            event.status == "cancelled"
            or not live_subject
            or (locked.replacement_range_instance_id, locked.replacement_request_id) != replacement
            or not _recovery_pointer_matches(current, expected_old, replacement, cleared_old)
        ):
            raise _range_error(
                "Replacement generation changed or was cancelled", category=RecoveryFailureCategory.INTERNAL_ERROR
            )
        if (current.range_instance_id, current.range_request_id) != replacement:
            current.range_instance_id = replacement_id
            current.range_request_id = replacement_request
            current.range_status = replacement_status
            current.save(update_fields=["range_instance_id", "range_request_id", "range_status", "updated_at"])
        participant.range_instance_id = current.range_instance_id
        participant.range_request_id = current.range_request_id
        participant.range_status = current.range_status
        locked.phase = recovery.phase = RecoveryPhase.PARTICIPANT_REPOINTED.value
        locked.save(update_fields=["phase", "updated_at"])


def _complete_recovery(
    recovery: CTFRangeRecovery,
    participant: CTFParticipant,
    operator: object | None,
) -> None:
    """Write the completion audit row and mark `recovery` as `COMPLETED`."""
    audit_range_recovery(
        actor_id=operator,
        recovery=recovery,
        participant=participant,
        previous_status=ResourceStatus.DESTROYING.value,
    )
    recovery.phase = RecoveryPhase.COMPLETED.value
    recovery.save(update_fields=["phase", "updated_at"])


def _spare_generation_eligible(candidate: CTFSpareRange, event: CTFEvent, scope: ResourceScope) -> bool:
    """Require live journal identity, ready runtime, transferable owner and exact event egress."""
    from ctf.bridges import (
        cms_get_range_status,
        cms_range_egress_compatible_with_event,
        cms_range_owner_reassignment_available,
    )
    from ctf.models import CTFRangeLaunch

    if candidate.range_instance_id is None or candidate.request_id is None:
        return False
    if not CTFRangeLaunch.objects.filter(
        pk=candidate.request_id,
        event=event,
        subject_kind="spare",
        subject_uuid=candidate.pk,
        range_instance_id=candidate.range_instance_id,
    ).exists():
        return False
    ready = candidate.status == SpareRangeStatus.READY.value or (
        cms_get_range_status(candidate.range_instance_id) == ResourceStatus.READY.value
    )
    return (
        ready
        and cms_range_owner_reassignment_available(candidate.range_instance_id)
        and cms_range_egress_compatible_with_event(candidate.range_instance_id, scope)
    )


def _live_replacement_subject(
    intent: CTFRangeLaunch | None,
    recovery: CTFRangeRecovery,
    participant: CTFParticipant,
    replacement: tuple[int, UUID],
) -> bool:
    """The exact native replacement must still belong to this recovery or its consumed spare."""
    if intent is None:
        return False
    if intent.subject_kind == "rebuild":
        return intent.subject_uuid == recovery.pk
    return (
        intent.subject_kind == "spare"
        and CTFSpareRange.objects.filter(
            pk=intent.subject_uuid,
            event_id=recovery.event_id,
            consumed_by=participant,
            status=SpareRangeStatus.CONSUMED.value,
            request_id=replacement[1],
            range_instance_id=replacement[0],
        ).exists()
    )


def _recovery_pointer_matches(
    current: CTFParticipant, expected_old: tuple[int, UUID | None], replacement: tuple[int, UUID], cleared_old: bool
) -> bool:
    """Accept only the exact old/replacement pointer or verified cleanup of that old generation."""
    return (current.range_instance_id, current.range_request_id) in {expected_old, replacement} or (
        cleared_old
        and current.range_instance_id is None
        and current.range_request_id == expected_old[1]
        and current.range_status == ""
    )
