"""Legacy user identifiers cannot admit principal-owned lifecycle callbacks."""

from uuid import uuid4

import pytest

from tests.cms.services.test_principal_range_authority import principal_range

__all__ = ["principal_range"]
pytestmark = [pytest.mark.django_db, pytest.mark.postgres]


def event_for(fixture):
    return {
        "event_type": "range.status.updated",
        "event_id": str(uuid4()),
        "timestamp": "2026-10-15T00:00:00Z",
        "request_id": str(fixture.request.request_id),
        "range_id": fixture.realized.pk,
        "user_id": None,
        "new_status": "failed",
        "error_message": "",
    }


def test_legacy_null_user_callback_cannot_mutate_principal_runtime(principal_range):
    from engine.handlers._range import _handle_status_updated

    _handle_status_updated(event_for(principal_range))
    principal_range.realized.refresh_from_db()
    assert principal_range.realized.status == "ready"


def test_unrecorded_callback_cannot_mutate_principal_cms_projection(principal_range):
    from cms.handlers.range_events import process_range_event

    process_range_event(event_for(principal_range))
    principal_range.instance.refresh_from_db()
    assert principal_range.instance.status == "ready"


def test_native_recorded_status_mirrors_accepted_state_and_remains_idempotent(principal_range):
    from cms.handlers.range_events import process_range_event
    from engine.models import RangeEventOutbox
    from engine.services._operation_apply_effects import _enqueue_range_status_event

    fixture = principal_range
    fixture.realized.status = "failed"
    fixture.realized.save(update_fields=["status"])
    _enqueue_range_status_event(fixture.realized, "failed", "")
    event = RangeEventOutbox.objects.get().payload
    process_range_event(event)
    fixture.instance.refresh_from_db()
    assert fixture.instance.status == "failed"

    process_range_event(event)
    fixture.instance.refresh_from_db()
    assert fixture.instance.status == "failed"


@pytest.mark.parametrize("change", ["payload", "generation", "operation", "owner"])
def test_stale_or_rewritten_callback_cannot_project_principal_state(principal_range, change):
    from cms.handlers.range_events import process_range_event
    from engine.models import RangeEventOutbox
    from engine.services._operation_apply_effects import _enqueue_range_status_event

    fixture = principal_range
    fixture.realized.status = "failed"
    fixture.realized.save(update_fields=["status"])
    _enqueue_range_status_event(fixture.realized, "failed", "")
    event = RangeEventOutbox.objects.get().payload
    if change == "payload":
        event["new_status"] = "destroyed"
    elif change == "generation":
        fixture.realized.resource_generation = uuid4()
        fixture.realized.save(update_fields=["resource_generation"])
    elif change == "operation":
        fixture.realized.provisioner_operation_id = uuid4()
        fixture.realized.save(update_fields=["provisioner_operation_id"])
    else:
        fixture.realized.owner_principal_uuid = uuid4()
        fixture.realized.save(update_fields=["owner_principal_uuid"])
    process_range_event(event)
    fixture.instance.refresh_from_db()
    assert fixture.instance.status == "ready"
