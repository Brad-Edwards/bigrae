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
from typing import Any
from uuid import UUID

from ctf.enums import SpareRangeStatus
from ctf.exceptions import CTFNotFoundError
from ctf.models import CTFEvent, CTFSpareRange
from ctf.services.audit import audit_spare_provisioning
from shared.log_sanitize import safe_log_value

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


def _provision_one_spare(event: CTFEvent, credential) -> CTFSpareRange:
    """Create one event-owned CMS range and record its CTF spare row.

    Mirrors ``ctf.services.range.provision.provision_participant_range``'s
    range_config extraction. Provisioning failures are recorded as a
    ``failed`` spare rather than raised, so one bad attempt does not abort
    the rest of a top-up.
    """
    from ctf.bridges import CTFRangeLaunchOptions, cms_create_range, cms_find_range_instance_id

    agents_by_os = event.range_config.get("agents_by_os", {}) if event.range_config else {}
    ngfw_enabled = event.range_config.get("ngfw_enabled", False) if event.range_config else False

    # PLAT-201: a spare occupies a range slot like any other, so it draws from
    # the same event budget. Keyed on the spare UUID, which is stable and known
    # before the range exists.
    from ctf.services.range.capacity import admit_range, release_range

    spare = CTFSpareRange.objects.create(event=event, status=SpareRangeStatus.PROVISIONING.value)
    draw_key = _spare_draw_key(spare.pk)
    admission = admit_range(event.pk, draw_key)
    if admission is not None and admission["blocking"]:
        logger.warning(
            "provision_event_spares: capacity refused a spare for event=%s codes=%s",
            safe_log_value(event.pk),
            admission["reason_codes"],
        )
        spare.status = SpareRangeStatus.FAILED.value
        spare.save(update_fields=["status", "updated_at"])
        return spare

    try:
        from ctf.services.range.model_allocation import project_event_model_scope
        from management.services import resolve_principal_uuid
        from shared.model_access import OwnedReference
        from workspaces.services import resource_scope_from_ids

        owner = resolve_principal_uuid(event.owner_principal_uuid)
        scope = resource_scope_from_ids(
            kind=event.scope_kind,
            account_id=event.account_id,
            organization_id=event.organization_id,
            workspace_id=event.workspace_id,
        )

        subject = OwnedReference(owner="ctf", reference=f"draw:{spare.pk}")
        result = cms_create_range(
            scenario=event.scenario_id,
            agents_by_os=agents_by_os,
            ngfw_enabled=ngfw_enabled,
            remote_access_teardown_at=event.get_cleanup_time(),
            launch_options=CTFRangeLaunchOptions(
                model_admission_subject=subject,
                model_launch_scope=project_event_model_scope(
                    event,
                    draw_key,
                    subject,
                    spare_id=spare.pk,
                ),
                credential=credential,
                scope=scope,
                owner_principal=owner,
                event_uuid=event.pk,
            ),
        )
    except Exception:
        logger.exception(
            "provision_event_spares: range provisioning failed for event=%s",
            safe_log_value(event.pk),
        )
        # No range came up, so the draw must go back.
        release_range(draw_key)
        spare.status = SpareRangeStatus.FAILED.value
        spare.save(update_fields=["status", "updated_at"])
        return spare

    range_instance_id = cms_find_range_instance_id(result.request_id)
    spare.range_instance_id = range_instance_id
    spare.request_id = result.request_id
    spare.save(update_fields=["range_instance_id", "request_id", "updated_at"])
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
    from ctf.services.authorization import assert_event_action

    credential = event_actor_credential(operator)
    assert_event_action(credential, event, "event.manage_ranges")
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

    existing = CTFSpareRange.objects.filter(event=event, status__in=_ACTIVE_SPARE_STATUSES).count()
    to_create = max(0, target_count - existing)

    logger.info(
        "provision_event_spares: event=%s target=%s existing=%d creating=%d",
        safe_log_value(event_id),
        safe_log_value(target_count),
        existing,
        to_create,
    )

    created = 0
    for _ in range(to_create):
        spare = _provision_one_spare(event, credential)
        if spare.status != SpareRangeStatus.FAILED.value:
            created += 1

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

    credential = event_actor_credential(actor)
    assert_event_action(credential, event, "event.manage_ranges")
    unconsumed = CTFSpareRange.objects.filter(event=event, consumed_by__isnull=True).exclude(
        status=SpareRangeStatus.FAILED.value
    )

    destroyed = 0
    failed = 0

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
        spare.status = SpareRangeStatus.FAILED.value
        spare.save(update_fields=["status", "updated_at"])

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
