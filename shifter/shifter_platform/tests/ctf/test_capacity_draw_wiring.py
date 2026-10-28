"""Per-range capacity draws on the creation paths (PLAT-201, #680).

Every path that creates a range for an event now draws from that event's
budget: the participant path (including a late joiner arriving after the wave)
and the spare path. These drive the behaviour that matters -- the draw happens
before the range is created, an uncertain creation retains capacity until fenced
cancellation establishes absence, and an
enforcing over-budget draw refuses instead of failing later in spinup.
"""

from __future__ import annotations

import pytest

from ctf.exceptions import CTFRangeError

pytestmark = pytest.mark.django_db


def _summary(outcome: str, *, blocking: bool):
    return {"outcome": outcome, "blocking": blocking, "reason_codes": ["capacity.exceeds_headroom"]}


class TestParticipantPath:
    def test_draw_happens_before_the_range_is_created(
        self, ctf_event_active, participant_user, organizer_credential, monkeypatch
    ):
        """Ordering is the point: refuse before creating, not after."""
        from ctf.models import CTFParticipant
        from ctf.services.range.provision import provision_participant_range

        participant = CTFParticipant.objects.create(
            event=ctf_event_active,
            user=participant_user,
            email=participant_user.email,
            name="late",
            status="active",
        )
        order = []
        monkeypatch.setattr(
            "ctf.services.range.capacity.admit_range",
            lambda event_id, draw_key: order.append("admit") or None,
        )
        monkeypatch.setattr("ctf.services.range.capacity.release_range", lambda draw_key: None)

        def _record_create(**kwargs):
            order.append("create")
            raise RuntimeError("stop after ordering is observed")

        monkeypatch.setattr("ctf.bridges.cms_create_range", _record_create)

        with pytest.raises(CTFRangeError):
            provision_participant_range(participant.pk, actor=organizer_credential)

        assert order == ["admit", "create"]

    def test_blocking_draw_refuses_without_creating_a_range(
        self, ctf_event_active, participant_user, organizer_credential, monkeypatch
    ):
        from ctf.models import CTFParticipant
        from ctf.services.range.provision import provision_participant_range

        participant = CTFParticipant.objects.create(
            event=ctf_event_active,
            user=participant_user,
            email=participant_user.email,
            name="late",
            status="active",
        )
        created = []
        monkeypatch.setattr(
            "ctf.services.range.capacity.admit_range",
            lambda event_id, draw_key: _summary("rejected", blocking=True),
        )
        monkeypatch.setattr("ctf.bridges.cms_create_range", lambda **kwargs: created.append(1))

        with pytest.raises(CTFRangeError, match="capacity budget"):
            provision_participant_range(participant.pk, actor=organizer_credential)

        assert created == []

    def test_draw_is_keyed_on_the_participant(
        self, ctf_event_active, participant_user, organizer_credential, monkeypatch
    ):
        """A stable key is what makes a retried provision idempotent."""
        from ctf.models import CTFParticipant
        from ctf.services.range.provision import provision_participant_range

        participant = CTFParticipant.objects.create(
            event=ctf_event_active,
            user=participant_user,
            email=participant_user.email,
            name="late",
            status="active",
        )
        seen = []
        monkeypatch.setattr(
            "ctf.services.range.capacity.admit_range",
            lambda event_id, draw_key: seen.append(draw_key) or _summary("rejected", blocking=True),
        )

        with pytest.raises(CTFRangeError):
            provision_participant_range(participant.pk, actor=organizer_credential)

        assert seen == [participant.pk]

    def test_failed_creation_retains_draw_until_cancelled_request_is_absent(
        self, ctf_event_active, participant_user, organizer_credential, monkeypatch
    ):
        """An exception cannot prove absence; cancellation and lookup can."""
        from ctf.models import CTFParticipant, CTFRangeLaunch
        from ctf.services.range.lifecycle import cleanup_event_ranges
        from ctf.services.range.provision import provision_participant_range
        from engine.models import CapacityDraw
        from tests.ctf.test_cancelled_launch_cleanup import capacity_budget

        capacity_budget(ctf_event_active)
        participant = CTFParticipant.objects.create(
            event=ctf_event_active,
            user=participant_user,
            email=participant_user.email,
            name="late",
            status="active",
        )
        requests = []

        def _boom(**kwargs):
            requests.append(kwargs["launch_options"].request_uuid)
            raise RuntimeError("cms down")

        monkeypatch.setattr("ctf.bridges.cms_create_range", _boom)

        with pytest.raises(CTFRangeError, match="cms down"):
            provision_participant_range(participant.pk, actor=organizer_credential)

        participant.refresh_from_db()
        intent = CTFRangeLaunch.objects.get(pk=participant.range_request_id)
        assert intent.dispatched_at is None
        assert participant.range_instance_id is None
        assert CapacityDraw.objects.filter(draw_key=participant.pk, released_at=None).count() == 1
        with pytest.raises(CTFRangeError, match="cms down"):
            provision_participant_range(participant.pk, actor=organizer_credential)
        assert requests == [intent.pk, intent.pk]
        assert CTFRangeLaunch.objects.filter(subject_uuid=participant.pk).count() == 1
        assert CapacityDraw.objects.filter(draw_key=participant.pk, released_at=None).count() == 1

        # Real CMS lookup finds no reservation; the committed cancellation
        # prevents any retry from creating this generation after release.
        assert cleanup_event_ranges(ctf_event_active.pk, actor=organizer_credential)["failed"] == 0
        participant.refresh_from_db()
        assert CTFRangeLaunch.all_objects.get(pk=intent.pk).deleted_at is not None
        assert participant.range_request_id is None
        assert participant.range_instance_id is None
        assert not CapacityDraw.objects.filter(draw_key=participant.pk, released_at=None).exists()


class TestSparePath:
    def test_blocking_draw_marks_the_spare_failed_without_creating(self, ctf_event, organizer_credential, monkeypatch):
        from ctf.enums import SpareRangeStatus
        from ctf.services.range.spares import provision_event_spares

        created = []
        monkeypatch.setattr(
            "ctf.services.range.capacity.admit_range",
            lambda event_id, draw_key: _summary("rejected", blocking=True),
        )
        monkeypatch.setattr("ctf.services.range.capacity.assess_declared_capacity", lambda event_id, source: None)
        monkeypatch.setattr("ctf.bridges.cms_create_range", lambda **kwargs: created.append(1))

        result = provision_event_spares(ctf_event.pk, 2, operator=organizer_credential)

        assert created == []
        assert result["created"] == 0
        from ctf.models import CTFSpareRange

        assert CTFSpareRange.objects.filter(event=ctf_event, status=SpareRangeStatus.FAILED.value).count() == 2

    def test_each_spare_draws_under_a_distinct_key(self, ctf_event, organizer_credential, monkeypatch):
        """Two spares must not collide on one ledger key."""
        from ctf.services.range.spares import provision_event_spares

        seen = []
        monkeypatch.setattr(
            "ctf.services.range.capacity.admit_range",
            lambda event_id, draw_key: seen.append(draw_key) or _summary("rejected", blocking=True),
        )
        monkeypatch.setattr("ctf.services.range.capacity.assess_declared_capacity", lambda event_id, source: None)

        provision_event_spares(ctf_event.pk, 3, operator=organizer_credential)

        assert len(seen) == 3
        assert len(set(seen)) == 3
