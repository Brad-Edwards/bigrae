"""API-token scopes project to exact application-authorization ceilings."""

from __future__ import annotations

from uuid import UUID

from shared.api_tokens.scopes import (
    CTF_EVENT_READ,
    CTF_EVENT_WRITE,
    CTF_PLAY_READ,
    credential_ceiling,
)
from shared.authorization import TargetRef


def test_ctf_event_write_scope_admits_the_exact_organizer_actions() -> None:
    target = TargetRef("event", UUID("00000000-0000-0000-0000-000000000001"))
    ceiling = credential_ceiling([CTF_EVENT_WRITE], target=target)

    assert ceiling.permits("event.manage_ranges", target)
    assert ceiling.permits("event.manage_participants", target)
    assert ceiling.permits("event.manage_lifecycle", target)
    assert ceiling.permits("event.manage_staff", target)
    assert not ceiling.permits("range.manage", target)
    assert not ceiling.permits("event.participate", target)


def test_ctf_read_and_play_scopes_remain_separate() -> None:
    target = TargetRef("event", UUID("00000000-0000-0000-0000-000000000001"))
    read = credential_ceiling([CTF_EVENT_READ], target=target)
    play = credential_ceiling([CTF_PLAY_READ], target=target)

    assert read.permits("event.read", target)
    assert not read.permits("event.participate", target)
    assert play.permits("event.participate", target)
    assert "range.access" in play.actions
    assert not play.permits("event.manage_ranges", target)
