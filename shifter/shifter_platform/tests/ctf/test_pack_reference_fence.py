"""CTF event and schedule writers hold the CMS pack admission fence (ADR-034-R13, #2520)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from cms.models import TenantPackRemoval
from ctf.enums import EventStatus, ScheduledTaskType
from ctf.exceptions import CTFValidationError
from ctf.models import CTFEvent, CTFScheduledTask
from ctf.services.event import create_event, update_event
from ctf.services.range.tasks import request_event_provisioning

pytestmark = pytest.mark.django_db


def _removed(scenario_id, user):
    return TenantPackRemoval.objects.create(
        organization_uuid=uuid4(),
        scenario_id=scenario_id,
        package_name=scenario_id,
        package_version="1",
        package_digest="sha256:" + "a" * 64,
        requested_by=user,
        next_attempt_at=timezone.now(),
    )


def _event_data(scenario_id):
    start = timezone.now() + timedelta(days=1)
    return {
        "name": "Fenced event",
        "event_start": start,
        "event_end": start + timedelta(hours=4),
        "scenario_id": scenario_id,
    }


def test_event_and_schedule_writers_refuse_a_removed_pack(organizer_user, ctf_event_draft):
    _removed("removed-pack", organizer_user)

    with pytest.raises(CTFValidationError):
        create_event(organizer_user, _event_data("removed-pack"))
    with pytest.raises(CTFValidationError):
        update_event(ctf_event_draft.pk, {"scenario_id": "removed-pack"}, actor_id=organizer_user.pk)
    assert CTFEvent.objects.get(pk=ctf_event_draft.pk).scenario_id == "basic"

    ended = CTFEvent.objects.filter(pk=ctf_event_draft.pk)
    ended.update(scenario_id="removed-pack", status=EventStatus.ENDED.value)
    with pytest.raises(CTFValidationError):
        request_event_provisioning(ctf_event_draft.pk)
    assert not CTFScheduledTask.objects.filter(task_type=ScheduledTaskType.SPIN_UP_RANGES.value).exists()

    # A scenario that was never registered keeps the existing permissive behavior.
    assert create_event(organizer_user, _event_data("basic")).scenario_id == "basic"


def test_scenario_change_applies_create_time_scenario_authorization(
    organizer_user, ctf_event_draft, settings, monkeypatch
):
    settings.CTF_CONTENT_REFERENCES = {"private-content": object()}
    monkeypatch.setattr("ctf.bridges.cms_list_scenarios", lambda user: [("basic", "Basic")])

    with pytest.raises(CTFValidationError):
        update_event(ctf_event_draft.pk, {"scenario_id": "private-content"}, actor_id=organizer_user.pk)
    assert CTFEvent.objects.get(pk=ctf_event_draft.pk).scenario_id == "basic"
    # Unchanged scenarios are not re-authorized on unrelated edits.
    assert update_event(ctf_event_draft.pk, {"name": "Renamed"}, actor_id=organizer_user.pk).name == "Renamed"


def test_admin_cannot_create_or_reactivate_pack_references(admin_user, ctf_event):
    """Admin forms bypass the fenced services, so reference-writing fields stay read-only."""
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    from ctf.admin._event import CTFEventAdmin
    from ctf.admin._inlines import CTFScheduledTaskInline
    from ctf.admin._misc import CTFScheduledTaskAdmin

    request = RequestFactory().get("/admin/")
    request.user = admin_user
    event_admin = CTFEventAdmin(CTFEvent, site)
    task_admin = CTFScheduledTaskAdmin(CTFScheduledTask, site)
    inline = CTFScheduledTaskInline(CTFEvent, site)

    assert {"scenario_id", "status"} <= set(event_admin.get_readonly_fields(request, ctf_event))
    assert {"event", "task_type", "status"} <= set(task_admin.get_readonly_fields(request))
    assert {"task_type", "status"} <= set(inline.get_readonly_fields(request, ctf_event))
    assert not task_admin.has_add_permission(request)
    assert not inline.has_add_permission(request, ctf_event)
