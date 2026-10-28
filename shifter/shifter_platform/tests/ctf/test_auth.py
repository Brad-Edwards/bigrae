"""Tests for CTF authentication, routing, and access control.

Tests cover:
- OIDC backend extension for CTF user types
- Dashboard routing by user type
- Access control decorators
- Dedicated temporary-account authentication boundaries
- CTF context processor
- Dev auth CTF user type support

All tests run WITHOUT @pytest.mark.django_db by mocking the ORM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory, override_settings

from shared.auth import (
    CTF_ORGANIZER_GROUP,
    CTF_PARTICIPANT_GROUP,
)

# ---------------------------------------------------------------------------
# Mock User Helpers
# ---------------------------------------------------------------------------


class _MockGroupManager:
    """Simulates user.groups with in-memory set for filter/add/remove/clear."""

    def __init__(self, group_names: set[str] | None = None):
        self._groups = set(group_names or ())

    def filter(self, *, name=None, name__in=None):
        """Return a queryset-like object for group filtering."""
        if name is not None:
            matched = {name} & self._groups
        elif name__in is not None:
            matched = set(name__in) & self._groups
        else:
            matched = set(self._groups)
        return _MockGroupQS(matched, self)

    def add(self, *groups):
        for g in groups:
            self._groups.add(g.name if hasattr(g, "name") else g)

    def remove(self, *groups):
        for g in groups:
            name = g.name if hasattr(g, "name") else g
            self._groups.discard(name)

    def clear(self):
        self._groups.clear()

    def values_list(self, field, flat=False):
        """Simulate values_list('name', flat=True)."""
        return list(self._groups)


class _MockGroupQS:
    """Mimics a filtered Group queryset."""

    def __init__(self, names: set[str], manager: _MockGroupManager):
        self._names = names
        self._manager = manager

    def exists(self):
        return bool(self._names)

    def __iter__(self):
        for n in self._names:
            yield _MockGroup(n)

    def __bool__(self):
        return bool(self._names)


class _MockGroup:
    """Minimal Group stand-in."""

    def __init__(self, name: str):
        self.name = name


def _make_mock_user(
    *,
    email: str = "test@test.com",
    groups: set[str] | None = None,
    is_active: bool = True,
    is_staff: bool = False,
    is_superuser: bool = False,
    is_authenticated: bool = True,
    pk: int = 1,
):
    """Create a mock user with in-memory group management."""
    user = MagicMock()
    user.pk = pk
    user.id = pk
    user.email = email
    user.username = email
    user.is_active = is_active
    user.is_staff = is_staff
    user.is_superuser = is_superuser
    user.is_authenticated = is_authenticated
    user.groups = _MockGroupManager(groups)
    return user


# ---------------------------------------------------------------------------
# Shared Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def request_factory() -> RequestFactory:
    """Django request factory."""
    return RequestFactory()


@pytest.fixture
def mock_organizer_user():
    """Mock user in CTF Organizer group."""
    return _make_mock_user(
        email="organizer@test.com",
        groups={CTF_ORGANIZER_GROUP},
        pk=10,
    )


@pytest.fixture
def mock_participant_user():
    """Mock user in CTF Participant group."""
    return _make_mock_user(
        email="participant@test.com",
        groups={CTF_PARTICIPANT_GROUP},
        pk=20,
    )


@pytest.fixture
def mock_standard_user():
    """Mock user with no CTF groups."""
    return _make_mock_user(
        email="standard@test.com",
        groups=set(),
        pk=30,
    )


@pytest.fixture
def mock_profile():
    """Reusable mock profile factory."""

    def _make(user_type="standard", active_ctf_event_id=None):
        profile = MagicMock()
        profile.user_type = user_type
        profile.active_ctf_event_id = active_ctf_event_id
        return profile

    return _make


# ---------------------------------------------------------------------------
# OIDC Backend Extension Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOIDCBackendCTFUserType:
    """OIDC backend turns custom:user_type claims into CTF group membership.

    Behavior tests against real users, groups, and profiles through the
    backend's ``_update_user_type`` entry point. The sync mechanics (audit
    rows, add-only CTF membership, the CTF-only invariant) are unit-tested in
    ``tests/config/test_user_type_sync.py``; these tests cover the OIDC
    integration path and the ``custom:ctf_event_id`` plumbing.
    """

    def _make_backend(self):
        from config.oidc import ShifterOIDCBackend

        return ShifterOIDCBackend()

    @pytest.fixture
    def real_user(self, db):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(username="newctf@test.com", email="newctf@test.com")

    def test_organizer_claim_does_not_add_organizer_group(self, real_user):
        # #1516: a self-service custom:user_type claim can never grant the CTF
        # Organizer group. Organizer authority is administrator-controlled only.
        self._make_backend()._update_user_type(real_user, {"custom:user_type": "ctf_organizer"})
        assert not real_user.groups.filter(name=CTF_ORGANIZER_GROUP).exists()

    def test_participant_claim_adds_participant_group(self, real_user):
        self._make_backend()._update_user_type(real_user, {"custom:user_type": "ctf_participant"})
        assert real_user.groups.filter(name=CTF_PARTICIPANT_GROUP).exists()

    def test_missing_claim_leaves_groups_unchanged(self, real_user):
        self._make_backend()._update_user_type(real_user, {"sub": "some-sub"})
        assert not real_user.groups.filter(name__in=[CTF_ORGANIZER_GROUP, CTF_PARTICIPANT_GROUP]).exists()

    def test_invalid_claim_value_ignored(self, real_user):
        self._make_backend()._update_user_type(real_user, {"custom:user_type": "invalid_type"})
        assert not real_user.groups.filter(name__in=[CTF_ORGANIZER_GROUP, CTF_PARTICIPANT_GROUP]).exists()

    def test_ctf_event_set_for_participant_from_claim(self, real_user):
        from datetime import timedelta

        from django.utils import timezone

        from ctf.enums import EventStatus
        from ctf.models import CTFEvent

        event = CTFEvent.objects.create(
            name="OIDC Event",
            description="event",
            created_by=real_user,
            status=EventStatus.ACTIVE.value,
            event_start=timezone.now() - timedelta(hours=1),
            event_end=timezone.now() + timedelta(hours=7),
            scenario_id="basic",
        )

        self._make_backend()._update_user_type(
            real_user,
            {"custom:user_type": "ctf_participant", "custom:ctf_event_id": str(event.pk)},
        )

        real_user.refresh_from_db()
        assert real_user.groups.filter(name=CTF_PARTICIPANT_GROUP).exists()
        assert real_user.profile.active_ctf_event_id == event.pk

    def test_invalid_ctf_event_id_ignored(self, real_user):
        self._make_backend()._update_user_type(
            real_user,
            {"custom:user_type": "ctf_participant", "custom:ctf_event_id": "not-a-uuid"},
        )
        real_user.refresh_from_db()
        assert real_user.groups.filter(name=CTF_PARTICIPANT_GROUP).exists()
        assert real_user.profile.active_ctf_event_id is None


class TestDashboardRouting:
    """The stable dashboard URL serves the unified SPA to authenticated users."""

    def _call_dashboard_router(self, request_factory, user):
        """Call dashboard_router view directly with a mock user."""
        from config.views import dashboard_router

        request = request_factory.get("/dashboard/")
        request.user = user
        return dashboard_router(request)

    def test_standard_user_gets_spa(self, request_factory, mock_standard_user):
        response = self._call_dashboard_router(request_factory, mock_standard_user)
        assert response.status_code == 302
        assert response.url == "/"

    def test_organizer_gets_spa(self, request_factory, mock_organizer_user):
        response = self._call_dashboard_router(request_factory, mock_organizer_user)
        assert response.status_code == 302
        assert response.url == "/"

    def test_participant_gets_spa(self, request_factory, mock_participant_user):
        response = self._call_dashboard_router(request_factory, mock_participant_user)
        assert response.status_code == 302
        assert response.url == "/"

    def test_unauthenticated_redirected_to_login(self, request_factory):
        """Unauthenticated users should be redirected to login."""
        from config.views import dashboard_router

        anon = _make_mock_user(is_authenticated=False)
        request = request_factory.get("/dashboard/")
        request.user = anon
        response = dashboard_router(request)
        assert response.status_code == 302

    def test_user_without_profile_gets_spa(self, request_factory):
        user = _make_mock_user(email="noprofile@test.com", groups=set(), pk=99)
        response = self._call_dashboard_router(request_factory, user)
        assert response.status_code == 302
        assert response.url == "/"


@pytest.mark.django_db
class TestDevLogin:
    """Dev login CTF user type support (behavior tests, real DB).

    Drives the dev-login view through the test client and asserts the real
    group membership and redirect. The shared sync mechanics and audit trail
    are unit-tested in ``tests/config/test_user_type_sync.py``.
    """

    @override_settings(DEBUG=True)
    def test_dev_login_ctf_organizer(self, client):
        """Dev login should add user to CTF Organizer group."""
        from django.contrib.auth import get_user_model

        response = client.post("/dev-login/", {"email": "ctforg@test.com", "user_type": "ctf_organizer"})

        assert response.status_code == 302
        assert "/ctf/admin/" in response.url
        user = get_user_model().objects.get(username="ctforg@test.com")
        assert user.groups.filter(name=CTF_ORGANIZER_GROUP).exists()

    @override_settings(DEBUG=True)
    def test_dev_login_ctf_participant(self, client):
        """Dev login should add user to CTF Participant group."""
        from django.contrib.auth import get_user_model

        response = client.post("/dev-login/", {"email": "ctfpart@test.com", "user_type": "ctf_participant"})

        assert response.status_code == 302
        assert "/mission-control/" in response.url
        user = get_user_model().objects.get(username="ctfpart@test.com")
        assert user.groups.filter(name=CTF_PARTICIPANT_GROUP).exists()

    @override_settings(DEBUG=True)
    def test_dev_login_standard_user_default(self, client):
        """Dev login without user_type should default to standard (no CTF groups)."""
        from django.contrib.auth import get_user_model

        response = client.post("/dev-login/", {"email": "dev@example.com"})

        assert response.status_code == 302
        assert "/mission-control/" in response.url
        user = get_user_model().objects.get(username="dev@example.com")
        assert not user.groups.filter(name__in=[CTF_ORGANIZER_GROUP, CTF_PARTICIPANT_GROUP]).exists()
