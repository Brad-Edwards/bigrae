"""Tests for CTF event range provisioning requests and progress.

Covers ``request_event_provisioning`` (enqueue or coalesce one SPIN_UP_RANGES
task) and ``get_provision_progress`` (participant counts plus the active task).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from ctf.enums import ScheduledTaskStatus, ScheduledTaskType
from ctf.exceptions import CTFNotFoundError
from ctf.models import CTFParticipant, CTFScheduledTask
from ctf.services import range as range_service


@pytest.mark.django_db
class TestRequestEventProvisioning:
    """request_event_provisioning enqueues/coalesces a SPIN_UP_RANGES task."""

    def _spin_up_count(self, event):
        return CTFScheduledTask.objects.filter(
            event=event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
        ).count()

    def test_creates_due_now_task_when_none_exists(self, ctf_event):
        before = timezone.now()

        task = range_service.request_event_provisioning(ctf_event.id, source="manual")

        assert task.task_type == ScheduledTaskType.SPIN_UP_RANGES.value
        assert task.status == ScheduledTaskStatus.PENDING.value
        assert before - timedelta(seconds=2) <= task.scheduled_for <= timezone.now()
        assert task.metadata.get("source") == "manual"
        assert self._spin_up_count(ctf_event) == 1

    def test_coalesces_future_pending_pulled_to_now(self, ctf_event):
        existing = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now() + timedelta(hours=6),
        )

        task = range_service.request_event_provisioning(ctf_event.id, source="manual")

        assert task.pk == existing.pk
        task.refresh_from_db()
        assert task.scheduled_for <= timezone.now()
        assert task.metadata.get("source") == "manual"
        # No duplicate runnable task was created.
        assert self._spin_up_count(ctf_event) == 1

    def test_reuses_running_task(self, ctf_event):
        existing = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now() - timedelta(minutes=1),
            status=ScheduledTaskStatus.RUNNING.value,
        )

        task = range_service.request_event_provisioning(ctf_event.id)

        assert task.pk == existing.pk
        assert task.status == ScheduledTaskStatus.RUNNING.value
        assert self._spin_up_count(ctf_event) == 1

    def test_event_not_found_raises(self):
        uuid4_2 = uuid4()
        with pytest.raises(CTFNotFoundError):
            range_service.request_event_provisioning(uuid4_2)


@pytest.mark.django_db
class TestGetProvisionProgress:
    """get_provision_progress projects participant counts plus the active task."""

    def _participant(self, event, name, range_status, range_instance_id=None):
        return CTFParticipant.objects.create(
            event=event,
            email=f"{name}@test.com",
            name=name,
            range_status=range_status,
            range_instance_id=range_instance_id,
        )

    def test_counts_and_active_task(self, ctf_event):
        self._participant(ctf_event, "a", "ready", 1)
        self._participant(ctf_event, "b", "provisioning", 2)
        self._participant(ctf_event, "c", "error")
        self._participant(ctf_event, "d", "")  # not assigned
        task = CTFScheduledTask.objects.create(
            event=ctf_event,
            task_type=ScheduledTaskType.SPIN_UP_RANGES.value,
            scheduled_for=timezone.now(),
        )

        progress = range_service.get_provision_progress(ctf_event.id)

        counts = progress["counts"]
        assert counts["total"] == 4
        assert counts["ready"] == 1
        assert counts["provisioning"] == 1
        assert counts["error"] == 1
        assert counts["not_assigned"] == 1
        assert progress["task"]["id"] == str(task.pk)
        assert progress["task"]["status"] == ScheduledTaskStatus.PENDING.value

    def test_no_active_task(self, ctf_event):
        progress = range_service.get_provision_progress(ctf_event.id)

        assert progress["task"] is None
        assert progress["counts"]["total"] == 0
