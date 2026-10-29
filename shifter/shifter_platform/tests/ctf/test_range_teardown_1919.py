"""Regression: CTF range teardown retains linkage/capacity until verified cleanup (#1919, ADR-063-R4/R5).

Dispatch is not verified destruction: releasing capacity or clearing the linkage
at dispatch, or on a logical DESTROYED status without scoped provider
inventory/readback evidence, returns a reusable slot while resources may remain.
Release happens only on a terminal DESTROYED transition carrying
``cleanup_verified=True``.
"""

from __future__ import annotations

import pytest

from ctf.models import CTFParticipant
from ctf.services.range import lifecycle
from ctf.signals import sync_ctf_participant_range_status
from shared.enums import ResourceStatus

pytestmark = pytest.mark.django_db


def _participant(event, user):
    return CTFParticipant.objects.create(
        event=event,
        user=user,
        email=user.email,
        name="racer",
        status="active",
        range_instance_id=42,
        range_status="ready",
    )


def _fire(new_status, *, cleanup_verified):
    sync_ctf_participant_range_status(
        sender=None,
        range_instance_id=42,
        new_status=new_status,
        previous_status="destroying",
        cleanup_verified=cleanup_verified,
    )


def test_dispatch_retains_linkage_and_capacity_until_terminal(
    ctf_event_active, participant_user, organizer_credential, monkeypatch
):
    participant = _participant(ctf_event_active, participant_user)
    released: list = []
    monkeypatch.setattr("ctf.bridges.cms_release_range_capacity", lambda draw_key: released.append(draw_key))
    monkeypatch.setattr(
        "ctf.bridges.cms_destroy_recovery_range", lambda credential, event_uuid, range_instance_id: None
    )

    result = lifecycle.destroy_participant_range(participant.pk, actor=organizer_credential)

    participant.refresh_from_db()
    assert participant.range_instance_id == 42
    assert participant.range_status == "destroying"
    assert released == []
    assert result["status"] == "destroying"


def test_verified_terminal_releases_capacity_and_clears_linkage(
    ctf_event_active, participant_user, organizer_credential, monkeypatch
):
    participant = _participant(ctf_event_active, participant_user)
    released: list = []
    monkeypatch.setattr("ctf.bridges.cms_release_range_capacity", lambda draw_key: released.append(draw_key))
    monkeypatch.setattr(
        "ctf.bridges.cms_destroy_recovery_range", lambda credential, event_uuid, range_instance_id: None
    )
    lifecycle.destroy_participant_range(participant.pk, actor=organizer_credential)

    _fire(ResourceStatus.DESTROYED.value, cleanup_verified=True)

    participant.refresh_from_db()
    assert participant.range_instance_id is None
    assert participant.range_status == ""
    assert released == [participant.pk]


def test_unverified_terminal_retains_capacity_and_linkage(
    ctf_event_active, participant_user, organizer_credential, monkeypatch
):
    participant = _participant(ctf_event_active, participant_user)
    released: list = []
    monkeypatch.setattr("ctf.bridges.cms_release_range_capacity", lambda draw_key: released.append(draw_key))
    monkeypatch.setattr(
        "ctf.bridges.cms_destroy_recovery_range", lambda credential, event_uuid, range_instance_id: None
    )
    lifecycle.destroy_participant_range(participant.pk, actor=organizer_credential)

    # Logical DESTROYED without inventory evidence must not release a reusable slot.
    _fire(ResourceStatus.DESTROYED.value, cleanup_verified=False)

    participant.refresh_from_db()
    assert participant.range_instance_id == 42
    # The status projection still advances (the elif branch), only the linkage/capacity is retained.
    assert participant.range_status == ResourceStatus.DESTROYED.value
    assert released == []


def test_destroy_single_range_skips_when_no_range_instance(
    ctf_event_active, participant_user, organizer_credential, monkeypatch
):
    """No range assigned -> dispatch is skipped and reported not-dispatched (lifecycle.py:205)."""
    dispatched: list = []
    monkeypatch.setattr(
        "ctf.bridges.cms_destroy_recovery_range",
        lambda credential, event_uuid, range_instance_id: dispatched.append(range_instance_id),
    )
    participant = CTFParticipant.objects.create(
        event=ctf_event_active,
        user=participant_user,
        email=participant_user.email,
        name="racer",
        status="active",
        range_instance_id=None,
        range_status="",
    )

    assert lifecycle._destroy_single_range(participant, organizer_credential) is False
    assert dispatched == []


def test_destroy_single_range_forwards_event_authority(
    ctf_event_active, participant_user, organizer_credential, monkeypatch
):
    """The private dispatch helper carries the exact event with the credential."""
    dispatched: list = []
    monkeypatch.setattr(
        "ctf.bridges.cms_destroy_recovery_range",
        lambda credential, event_uuid, range_instance_id: dispatched.append(
            (credential, event_uuid, range_instance_id)
        ),
    )
    participant = _participant(ctf_event_active, participant_user)  # range_instance_id=42

    assert lifecycle._destroy_single_range(participant, organizer_credential) is True
    assert dispatched == [(organizer_credential, ctf_event_active.pk, 42)]
