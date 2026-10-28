"""Clean cancelled launch generations even before their subject pointer exists."""

import logging

from django.db import transaction
from django.db.models import Q

from ctf.models import CTFEvent, CTFParticipant, CTFRangeLaunch, CTFRangeRecovery, CTFSpareRange

logger = logging.getLogger(__name__)


def cleanup_cancelled_launches(event_id, actor, intents):
    """Return accepted CMS cleanup requests, failures and already-covered IDs."""
    from ctf.bridges import cms_destroy_recovery_range, cms_find_range_instance_id
    from ctf.services.authorization import admit_event_actions

    event = CTFEvent.objects.get(pk=event_id)
    admission = admit_event_actions(actor, event, "event.manage_ranges")
    dispatched = failed = 0
    covered = set()
    for intent in intents:
        try:
            instance_id = cms_find_range_instance_id(intent.pk)
            if instance_id is not None:
                covered.add(instance_id)
            absent = instance_id is None
            if not absent:
                absent = cms_destroy_recovery_range(actor, event_id, None, request_id=intent.pk)
                dispatched += 1
            if absent:
                _release_absent_claim(admission, intent, instance_id)
            elif intent.subject_kind == "participant":
                CTFParticipant.objects.filter(
                    pk=intent.subject_uuid, range_request_id=intent.pk, range_instance_id=instance_id
                ).update(range_status="destroying")
        except Exception:
            failed += 1
            logger.exception("Cancelled launch cleanup unavailable for request %s", intent.pk)
    return dispatched, failed, covered


def _release_absent_claim(admission, intent, instance_id, *, action="event.manage_ranges"):
    """The cancelled journal prevents creation; release only its current claim."""
    from ctf.services.authorization import assert_event_action
    from ctf.services.range.capacity import release_range
    from ctf.services.range.spares import _spare_draw_key

    with transaction.atomic():
        event = CTFEvent.all_objects.select_for_update().get(pk=intent.event_id)
        assert_event_action(admission, event, action)
        locked = CTFRangeLaunch.all_objects.select_for_update().get(pk=intent.pk, deleted_at__isnull=False)
        participant_id = None
        if locked.subject_kind == "participant":
            participant_id = locked.subject_uuid
        elif locked.subject_kind == "spare":
            spare = (
                CTFSpareRange.all_objects.select_for_update()
                .filter(
                    Q(range_instance_id=None) | Q(range_instance_id=instance_id),
                    pk=locked.subject_uuid,
                    event=event,
                    request_id=locked.pk,
                )
                .first()
            )
            if spare is not None:
                spare.status = "failed"
                spare.save(update_fields=["status", "updated_at"])
                release_range(_spare_draw_key(spare.pk), strict=True)
                participant_id = spare.consumed_by_id
        elif locked.subject_kind == "rebuild":
            recovery = (
                CTFRangeRecovery.all_objects.select_for_update()
                .filter(
                    Q(replacement_range_instance_id=None) | Q(replacement_range_instance_id=instance_id),
                    pk=locked.subject_uuid,
                    event=event,
                    replacement_request_id=locked.pk,
                )
                .first()
            )
            if recovery is not None:
                participant_id = recovery.participant_id
        if participant_id is not None:
            subject = (
                CTFParticipant.all_objects.select_for_update()
                .filter(
                    Q(range_instance_id=None) | Q(range_instance_id=instance_id),
                    pk=participant_id,
                    event=event,
                    range_request_id=locked.pk,
                )
                .first()
            )
            if subject is not None:
                subject.range_request_id = None
                subject.range_instance_id = None
                subject.range_status = ""
                subject.save(update_fields=["range_request_id", "range_instance_id", "range_status", "updated_at"])
                release_range(subject.pk, strict=True)


def event_deletion_targets(event, actor, cancelled_intents):
    """Settle fenced absent requests and retain every realized cleanup target."""
    from ctf.bridges import cms_find_range_instance_id
    from ctf.services.authorization import admit_event_actions

    admission = admit_event_actions(actor, event, "event.delete")
    ranges = set(
        CTFParticipant.all_objects.filter(event=event, range_instance_id__isnull=False).values_list(
            "range_instance_id", flat=True
        )
    )
    ranges.update(
        CTFSpareRange.all_objects.filter(event=event, range_instance_id__isnull=False).values_list(
            "range_instance_id", flat=True
        )
    )
    requests = set(
        CTFSpareRange.all_objects.filter(event=event, range_instance_id=None, request_id__isnull=False).values_list(
            "request_id", flat=True
        )
    )
    cancelled = {intent.pk: intent for intent in cancelled_intents}
    requests.update(cancelled)
    unresolved = set()
    for request_id in requests:
        instance_id = cms_find_range_instance_id(request_id)
        if instance_id is not None:
            ranges.add(instance_id)
        elif request_id in cancelled:
            # The event lock fenced creation before absence was observed.
            _release_absent_claim(admission, cancelled[request_id], None, action="event.delete")
        else:
            # No durable cancellation fence: the owning service must refuse or
            # retain cleanup. Absence alone cannot justify releasing the claim.
            unresolved.add(request_id)
    return [(instance_id, None) for instance_id in sorted(ranges)] + [
        (None, request_id) for request_id in sorted(unresolved)
    ]
