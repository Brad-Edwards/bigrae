"""Principal-native event spare-range pool (issue #1018, #2321).

Each spare is held by the event owner's principal in the event's account scope
until recovery transfers ownership to a participant principal. No placeholder
or inactive Django user is created for pooled infrastructure.

A spare's ``status`` starts at ``provisioning`` and is flipped to ``ready``/
``failed`` by :func:`ctf.signals.sync_ctf_spare_range_status` when CMS
reports the underlying range's status change (the same
``cms.services.range_status_changed`` projection that already keeps
``CTFParticipant.range_status`` in sync) -- no separate polling loop is
introduced here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ctf.enums import SpareRangeStatus
from ctf.exceptions import CTFNotFoundError
from ctf.models import CTFEvent, CTFSpareRange
from ctf.services.audit import audit_spare_provisioning
from shared.log_sanitize import safe_log_value

if TYPE_CHECKING:
    from ctf.models import CTFRangeLaunch
    from ctf.services.actors import EventActionAdmission
    from shared.credentials import CredentialContext

_EVENT_MANAGE_RANGES = "event.manage_ranges"

logger = logging.getLogger(__name__)

# A spare counts against the pool while it is still usable or on its way to
# being usable; failed/consumed spares are excluded from "existing" counts so
# a top-up replaces them.
_ACTIVE_SPARE_STATUSES = (SpareRangeStatus.PROVISIONING.value, SpareRangeStatus.READY.value)

# Fixed namespace so a spare's capacity draw key is derived deterministically
# from its own UUID and never collides with a participant id.
_SPARE_DRAW_NAMESPACE = UUID("6f9d1f5e-0f3a-4d33-9a2f-2f1c9c4a7b10")


def _get_event(event_id: UUID) -> CTFEvent:
    """Look up a `CTFEvent` by id, raising `CTFNotFoundError` if it does not exist."""
    try:
        return CTFEvent.objects.get(pk=event_id)
    except CTFEvent.DoesNotExist:
        raise CTFNotFoundError(
            f"Event {event_id} not found",
            details={"event_id": str(event_id)},
        ) from None


def _spare_draw_key(spare_id: UUID) -> UUID:
    """Return the stable capacity draw key for one spare.

    The spare row is created once and never reused, so its UUID is a stable
    identity for the draw. It is hashed into a separate UUID namespace because
    the ledger key is shared with participant-scoped draws.
    """
    from uuid import uuid5

    return uuid5(_SPARE_DRAW_NAMESPACE, str(spare_id))


def _reserve_spare_launch(
    event: CTFEvent, event_admission: EventActionAdmission, spare: CTFSpareRange
) -> CTFRangeLaunch | None:
    """Record a cancellable generation while the event and spare are locked."""
    from ctf.models import CTFRangeLaunch

    if spare.status != SpareRangeStatus.PROVISIONING.value or spare.consumed_by_id is not None:
        return None
    intent = CTFRangeLaunch.all_objects.filter(pk=spare.request_id).first() if spare.request_id else None
    if intent is not None:
        return _replay_spare_launch(intent, event_admission)
    return _reserve_new_spare_launch(event, event_admission, spare)


def _provision_one_spare(event: CTFEvent, credential: CredentialContext, spare: CTFSpareRange) -> CTFSpareRange:
    """Resume the spare's one reserved generation, retaining failures for retry."""
    from django.db import transaction

    from ctf.services.authorization import admit_event_actions, assert_event_action
    from ctf.services.range.launch_intents import execute_launch

    event_admission = admit_event_actions(credential, event, _EVENT_MANAGE_RANGES)
    with transaction.atomic():
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk)
        assert_event_action(event_admission, locked_event, _EVENT_MANAGE_RANGES)
        spare = CTFSpareRange.objects.select_for_update().get(pk=spare.pk, event=locked_event)
        intent = _reserve_spare_launch(locked_event, event_admission, spare)
    if intent is None:
        return spare
    try:
        execute_launch(intent)
    except Exception:
        # Keep the generation and its draw: CMS may already have committed it.
        logger.exception("Spare launch pending for event=%s", safe_log_value(event.pk))
    spare.refresh_from_db()
    return spare


def provision_event_spares(event_id: UUID, target_count: int, *, operator: object | None = None) -> dict[str, Any]:
    """Top up an event's spare-range pool to ``target_count``.

    Sets ``event.spare_range_count = target_count`` and creates
    ``max(0, target_count - existing)`` new event-owned spares. ``existing``
    counts only non-consumed, non-failed
    spares, so a prior failed attempt is replaced rather than counted toward
    the target. Writes one audit row for the top-up action as a whole (not
    one per range -- CMS already audits each individual range creation).

    Args:
        event_id: UUID of the event.
        target_count: Desired pool size.
        operator: Operator initiating the top-up (audit actor), if any.

    Returns:
        Dict with ``event_id``, ``target_count``, ``existing``, ``created``.

    Raises:
        CTFNotFoundError: If the event does not exist.
    """
    from ctf.services.range.capacity import assess_declared_capacity, declare_event_capacity

    event = _get_event(event_id)
    if operator is None:
        raise ValueError("Spare provisioning authority unavailable")
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import admit_event_actions, assert_event_action

    credential = event_actor_credential(operator)
    event_admission = admit_event_actions(credential, event, _EVENT_MANAGE_RANGES)
    credential = event_actor_credential(event_admission)
    from django.db import transaction

    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().get(pk=event.pk)
        assert_event_action(event_admission, event, _EVENT_MANAGE_RANGES)
        if event.status == "cancelled":
            from ctf.exceptions import CTFStateError

            raise CTFStateError("Cancelled events cannot provision spares")
        event.spare_range_count = target_count
        event.save(update_fields=["spare_range_count", "updated_at"])
    # Declare AFTER persisting the new target: the declaration's
    # expected_concurrent_ranges is roster + spare pool, so declaring first
    # published the previous pool size and understated the peak by exactly the
    # change (PLAT-201 preflight). Still before any spare spins up.
    declare_event_capacity(event_id, source="spare_pool")
    # PLAT-201: growing the spare pool raises peak concurrent ranges, so a
    # top-up is a capacity-relevant change and gets the same admission check as
    # a participant wave.
    capacity = assess_declared_capacity(event_id, source="spare_pool")
    if capacity is not None and capacity["blocking"]:
        logger.warning(
            "provision_event_spares: capacity refused top-up for event=%s codes=%s",
            safe_log_value(event_id),
            capacity["reason_codes"],
        )
        return {
            "event_id": str(event.pk),
            "target_count": target_count,
            "existing": CTFSpareRange.objects.filter(event=event, status__in=_ACTIVE_SPARE_STATUSES).count(),
            "created": 0,
            # Bounded codes only. The partition name is deployment topology and
            # stays on the operator-only assessment record (PLAT-201 preflight).
            "capacity_reason_codes": capacity["reason_codes"],
            "refused": True,
        }

    from django.db import transaction
    from django.db.models import Q

    from ctf.models import CTFRangeLaunch

    with transaction.atomic():
        locked_event = CTFEvent.objects.select_for_update().get(pk=event.pk)
        assert_event_action(event_admission, locked_event, _EVENT_MANAGE_RANGES)
        if locked_event.status == "cancelled" or locked_event.spare_range_count != target_count:
            raise ValueError("Spare target changed during admission")
        existing = CTFSpareRange.objects.filter(event=event, status__in=_ACTIVE_SPARE_STATUSES).count()
        new_spares = [
            CTFSpareRange.objects.create(event=event, status=SpareRangeStatus.PROVISIONING.value)
            for _ in range(max(0, target_count - existing))
        ]
        pending_requests = CTFRangeLaunch.objects.filter(event=event, dispatched_at=None).values_list("pk", flat=True)
        pending = list(
            CTFSpareRange.objects.select_for_update().filter(
                Q(request_id__isnull=True) | Q(request_id__in=pending_requests),
                event=event,
                status=SpareRangeStatus.PROVISIONING.value,
            )
        )
        # No pending spare becomes visible without an intent that event cleanup
        # can cancel. The outer event lock also fences concurrent top-ups.
        for spare in pending:
            _reserve_spare_launch(locked_event, event_admission, spare)
    for spare in pending:
        _provision_one_spare(event, credential, spare)
    created = (
        CTFSpareRange.objects.filter(pk__in=[spare.pk for spare in new_spares])
        .exclude(status=SpareRangeStatus.FAILED.value)
        .count()
    )

    audit_spare_provisioning(
        actor_id=operator,
        event_id=event.pk,
        target_count=target_count,
        existing=existing,
        created=created,
    )

    return {
        "event_id": str(event.pk),
        "target_count": target_count,
        "existing": existing,
        "created": created,
    }


def get_event_spare_summary(event_id: UUID) -> dict[str, Any]:
    """Return spare-pool counts by status plus an available-for-assignment count.

    Bounded operator diagnostics for the admin surface (a later chunk
    consumes it) -- no range/provider detail beyond counts and ids.

    Raises:
        CTFNotFoundError: If the event does not exist.
    """
    event = _get_event(event_id)
    spares = CTFSpareRange.objects.filter(event=event)

    counts = {status.value: 0 for status in SpareRangeStatus}
    for status_value in spares.values_list("status", flat=True):
        counts[status_value] = counts.get(status_value, 0) + 1

    available = spares.filter(status=SpareRangeStatus.READY.value, consumed_by__isnull=True).count()

    return {
        "event_id": str(event.pk),
        "target_count": event.spare_range_count,
        "counts": counts,
        "available": available,
    }


def cleanup_event_spares(event_id: UUID, *, actor: object) -> dict[str, Any]:
    """Tear down every unconsumed spare range using the initiating credential.

    Consumed spares are left alone -- their range now belongs to a
    participant and is torn down (if at all) through the normal
    participant-range cleanup path, not here.

    Args:
        event_id: UUID of the event.

    Returns:
        Dict with counts of destroy dispatches and failures.

    Raises:
        CTFNotFoundError: If the event does not exist.
    """
    from ctf.bridges import cms_destroy_range, cms_find_range_instance_id

    event = _get_event(event_id)
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import assert_event_action
    from ctf.services.range.launch_cleanup import cleanup_cancelled_launches
    from ctf.services.range.launch_intents import cancel_event_launches

    credential = event_actor_credential(actor)
    assert_event_action(credential, event, _EVENT_MANAGE_RANGES)
    intents = cancel_event_launches(event_id, actor, spares_only=True)
    destroyed, failed, _ = cleanup_cancelled_launches(event_id, credential, intents)
    unconsumed = (
        CTFSpareRange.objects.filter(event=event, consumed_by__isnull=True)
        .exclude(status=SpareRangeStatus.FAILED.value)
        .exclude(pk__in=[intent.subject_uuid for intent in intents])
    )

    for spare in unconsumed:
        try:
            if spare.range_instance_id is None and spare.request_id is not None:
                spare.range_instance_id = cms_find_range_instance_id(spare.request_id)
                if spare.range_instance_id is not None:
                    spare.save(update_fields=["range_instance_id", "updated_at"])
            if spare.range_instance_id is None:
                failed += 1
                logger.warning(
                    "cleanup_event_spares: cannot destroy spare with missing range (event=%s)",
                    safe_log_value(event_id),
                )
                continue
            cms_destroy_range(credential, event.pk, spare.range_instance_id)
            destroyed += 1
        except Exception:
            failed += 1
            logger.exception(
                "cleanup_event_spares: failed to destroy spare range_instance_id=%s (event=%s)",
                spare.range_instance_id,
                safe_log_value(event_id),
            )
            # Preserve nonterminal status so a later cleanup attempt can retry.
            continue
        # A queued destroy is not verified absence. Preserve the draw and
        # cleanup retry until the terminal provider projection arrives.

    logger.info(
        "cleanup_event_spares: event=%s destroyed=%d failed=%d",
        safe_log_value(event_id),
        destroyed,
        failed,
    )

    return {
        "event_id": str(event.pk),
        "destroyed": destroyed,
        "failed": failed,
    }


def _replay_spare_launch(intent: CTFRangeLaunch, admission: EventActionAdmission) -> CTFRangeLaunch | None:
    """A cancelled spare cannot replay; an admitted live generation keeps its original credential."""
    from ctf.services.actors import event_actor_credential
    from ctf.services.range.launch_intents import assert_launch_replay

    if intent.deleted_at is not None:
        return None
    assert_launch_replay(intent, event_actor_credential(admission), intent.parameters)
    return intent


def _reserve_new_spare_launch(
    event: CTFEvent, event_admission: EventActionAdmission, spare: CTFSpareRange
) -> CTFRangeLaunch | None:
    """Commit one exact spare draw and generation while the caller holds event and spare locks."""
    from ctf.services.range.capacity import admit_range
    from ctf.services.range.launch_intents import launch_parameters, reserve_launch
    from ctf.services.range.model_allocation import project_event_model_scope
    from management.services import resolve_principal_uuid
    from shared.model_access import OwnedReference

    owner = resolve_principal_uuid(event.owner_principal_uuid)
    subject = OwnedReference(owner="ctf", reference=f"draw:{spare.pk}")
    draw_key = _spare_draw_key(spare.pk)
    parameters = launch_parameters(
        event, owner, subject, project_event_model_scope(event, draw_key, subject, spare_id=spare.pk)
    )
    admission = admit_range(event.pk, draw_key)
    if admission is not None and admission["blocking"]:
        spare.status = SpareRangeStatus.FAILED.value
        spare.save(update_fields=["status", "updated_at"])
        return None
    intent = reserve_launch(event, "spare", spare.pk, event_admission, parameters)
    spare.request_id = intent.pk
    spare.save(update_fields=["request_id", "updated_at"])
    return intent
