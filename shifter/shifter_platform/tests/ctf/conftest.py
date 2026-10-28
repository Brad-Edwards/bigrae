"""Pytest fixtures for CTF tests.

This module provides shared fixtures for all CTF test modules.
Uses pytest fixtures to avoid OOM issues from inline mocks.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone

from ctf.enums import (
    ChallengeCategory,
    ChallengeDifficulty,
    EventStatus,
    ParticipantStatus,
    ScheduledTaskStatus,
)
from ctf.models import (
    CTFBracket,
    CTFChallenge,
    CTFEvent,
    CTFFlag,
    CTFParticipant,
    CTFScheduledTask,
    CTFSubmission,
    CTFTeam,
)
from shared.auth import CTF_ORGANIZER_GROUP, CTF_PARTICIPANT_GROUP

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.test import Client

User = get_user_model()


def _map_legacy_test_event_scope(sender, instance, **kwargs):
    """Translate old direct-model test setup into an explicit account scope."""
    if (
        instance._state.adding
        and instance.created_by_id is not None
        and not instance.scope_kind
        and instance.account_id is None
        and instance.organization_id is None
        and instance.workspace_id is None
    ):
        for field, value in individual_event_scope(instance.created_by).items():
            setattr(instance, field, value)


def _legacy_decision(allowed: bool):
    from shared.authorization import AuthorizationDecision, DecisionKind

    return AuthorizationDecision(
        DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
        "policy_allowed" if allowed else "policy_denied",
    )


def _relationship_event_decision(request, relationships):
    from ctf.models import CTFEvent, CTFParticipant
    from management.models import Principal
    from shared.authorization.model import deny_relation_for_action, grant_relation_for_action

    events = CTFEvent.all_objects if request.action == "event.delete" else CTFEvent.objects
    event = events.filter(pk=request.target.uuid).first()
    principal = Principal.objects.select_related("user").filter(uuid=request.principal.uuid, is_active=True).first()
    if event is None or principal is None:
        return _legacy_decision(False)
    user = principal.user
    subject = f"principal:{principal.uuid}"
    target = f"event:{event.pk}"
    if (subject, deny_relation_for_action(request.action), target) in relationships:
        return _legacy_decision(False)
    if user is not None and user.is_superuser:
        return _legacy_decision(True)
    participates = CTFParticipant.objects.filter(
        event=event, principal_uuid=principal.uuid, deleted_at__isnull=True
    ).exists()
    if request.action == "event.participate" or (request.action == "event.read" and participates):
        return _legacy_decision(participates)
    if (subject, "direct_administrator", target) in relationships and (
        subject,
        "deny_administrator",
        target,
    ) not in relationships:
        return _legacy_decision(True)
    return _legacy_decision((subject, grant_relation_for_action(request.action), target) in relationships)


def _legacy_policy_check(request, relationships):
    from management.models import Principal
    from shared.authorization import AuthorizationRequest, TargetRef

    if request.action == "installation.use_personal_tokens":
        return _legacy_decision(True)
    if request.target.type == "event":
        return _relationship_event_decision(request, relationships)
    if request.target.type == "range":
        from cms.models import RangeInstance

        event_uuid = (
            RangeInstance.all_objects.filter(uuid=request.target.uuid).values_list("event_uuid", flat=True).first()
        )
        event_action = {
            "range.manage": "event.manage_ranges",
            "range.read": "event.participate",
            "range.access": "event.participate",
        }.get(request.action)
        if event_uuid is not None and event_action is not None:
            return _relationship_event_decision(
                AuthorizationRequest(
                    request.principal,
                    event_action,
                    TargetRef("event", event_uuid),
                    request.scope,
                    request.credential,
                ),
                relationships,
            )
        return _legacy_decision(False)
    principal = Principal.objects.select_related("user").filter(uuid=request.principal.uuid, is_active=True).first()
    user = None if principal is None else principal.user
    if request.target.type == "organization" and request.action == "organization.manage_model_sources":
        from workspaces.models import OrganizationMembership

        legacy_member = (
            user is not None
            and OrganizationMembership.objects.filter(
                organization__uuid=request.target.uuid, user=user, role__in=("owner", "admin")
            ).exists()
        )
        principal_member = OrganizationMembership.objects.filter(
            organization__uuid=request.target.uuid,
            principal_uuid=request.principal.uuid,
        ).exists()
        if legacy_member or principal_member:
            return _legacy_decision(True)
    organizer = False
    if (
        request.action.endswith(".create_event")
        and user is not None
        and user.groups.filter(name=CTF_ORGANIZER_GROUP).exists()
    ):
        from workspaces.models import Account, AccountMembership, OrganizationMembership, WorkspaceMembership

        if request.target.type == "account":
            organizer = (
                Account.objects.filter(
                    uuid=request.target.uuid,
                    individual_principal_uuid=request.principal.uuid,
                ).exists()
                or AccountMembership.objects.filter(
                    account__uuid=request.target.uuid,
                    principal_uuid=request.principal.uuid,
                ).exists()
            )
        elif request.target.type == "organization":
            organizer = (
                OrganizationMembership.objects.filter(
                    organization__uuid=request.target.uuid,
                    user=user,
                    role="admin",
                ).exists()
                or OrganizationMembership.objects.filter(
                    organization__uuid=request.target.uuid,
                    principal_uuid=request.principal.uuid,
                ).exists()
            )
        elif request.target.type == "workspace":
            organizer = (
                WorkspaceMembership.objects.filter(
                    workspace__uuid=request.target.uuid,
                    user=user,
                    role__in=("owner", "admin"),
                ).exists()
                or WorkspaceMembership.objects.filter(
                    workspace__uuid=request.target.uuid,
                    principal_uuid=request.principal.uuid,
                ).exists()
            )
    return _legacy_decision(bool(organizer or (user is not None and user.is_superuser)))


@pytest.fixture
def organizer_credential(client, organizer_user):
    """Persist and return the organizer's live browser-session credential."""
    from config.session_credentials import session_credential

    client.force_login(organizer_user)
    return session_credential(organizer_user, client.session, provider_check=False)


@pytest.fixture(autouse=True)
def legacy_ctf_matrix_through_policy(monkeypatch):
    """Run retained CTF behavior tests through the post-cutover policy port.

    These tests describe the incumbent permission matrix that S8 must preserve
    deliberately.  The fixture is intentionally test-only: it translates the
    old owner/staff/participant facts into policy decisions, while production
    code has no Django-role fallback.  Tests which need a narrower decision or
    evaluator failure replace the provider factory in their own fixture/body.
    """
    from types import SimpleNamespace

    from django.db.models.signals import post_save, pre_save

    from shared.authorization import port

    pre_save.connect(
        _map_legacy_test_event_scope,
        sender=CTFEvent,
        dispatch_uid="tests.ctf.explicit-legacy-event-scope",
        weak=False,
    )

    relationships = set()

    def seed_event_owner(sender, instance, created, **kwargs):
        if created and instance.owner_principal_uuid:
            relationships.add(
                (f"principal:{instance.owner_principal_uuid}", "direct_administrator", f"event:{instance.pk}")
            )

    post_save.connect(
        seed_event_owner,
        sender=CTFEvent,
        dispatch_uid="tests.ctf.explicit-event-owner-relationship",
        weak=False,
    )

    def write_relationships(change):
        grant = change.grant_tuple
        deny = change.deny_tuple
        grant_key = (grant.user, grant.relation, grant.object)
        deny_key = (deny.user, deny.relation, deny.object)
        if str(change.effect) == "grant":
            relationships.add(grant_key)
            if deny_key != grant_key:
                relationships.discard(deny_key)
        else:
            relationships.add(deny_key)
            relationships.discard(grant_key)

    def read_relationships(change):
        from shared.authorization import RelationshipState

        grant = change.grant_tuple
        deny_tuple = change.deny_tuple
        return RelationshipState(
            (grant.user, grant.relation, grant.object) in relationships,
            (deny_tuple.user, deny_tuple.relation, deny_tuple.object) in relationships and deny_tuple != grant,
        )

    provider = SimpleNamespace(
        store_id="test-store",
        model_id="test-model",
        check=lambda request: _legacy_policy_check(request, relationships),
        batch_check=lambda requests: tuple(_legacy_policy_check(request, relationships) for request in requests),
        write_relationships=write_relationships,
        read_relationships=read_relationships,
    )
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    yield
    pre_save.disconnect(
        sender=CTFEvent,
        dispatch_uid="tests.ctf.explicit-legacy-event-scope",
    )
    post_save.disconnect(
        sender=CTFEvent,
        dispatch_uid="tests.ctf.explicit-event-owner-relationship",
    )


# Explicit, clearly synthetic per-event shared credential for tests that need a
# stable login value. Production events default to unique generated credentials;
# individual tests clear the event override when exercising that path.
TEST_CTF_BOOTSTRAP_PASSWORD = "test-ctf-bootstrap-pw"  # nosec B105


# -----------------------------------------------------------------------------
# In-memory model builders (no DB required)
# -----------------------------------------------------------------------------


def make_ctf_event(**overrides) -> CTFEvent:
    """Build an in-memory CTFEvent without saving.

    Uses `created_by_id` to avoid Django FK descriptor validation.
    All defaults can be overridden via kwargs.
    """
    now = timezone.now()
    defaults = {
        "id": uuid4(),
        "name": "Test CTF Event",
        "created_by_id": 1,
        # Synthetic tenancy scope for in-memory (unsaved) events (ADR-051, #2048).
        # DB-backed fixtures resolve a real personal workspace instead.
        "workspace_id": 1,
        "status": EventStatus.REGISTRATION.value,
        "event_start": now + timedelta(days=1),
        "event_end": now + timedelta(days=1, hours=8),
        "scenario_id": "basic",
        "auto_cleanup": True,
        "cleanup_delay_hours": 24,
        "team_mode": False,
        "range_spinup_minutes": 30,
    }
    defaults.update(overrides)
    return CTFEvent(**defaults)


def individual_event_scope(user) -> dict[str, object]:
    """Return the real direct-account placement for a synthetic individual."""
    import workspaces.services as workspace_services
    from shared.principal_port import principal_for_user
    from workspaces.models import Account

    principal = principal_for_user(user)
    Account.objects.get_or_create(
        individual_principal_uuid=principal.uuid,
        defaults={"kind": Account.Kind.INDIVIDUAL, "name": f"Test account {user.pk}"},
    )
    scope = workspace_services.individual_account_scope(principal)
    binding = workspace_services.resolve_resource_scope(scope)
    return {
        "scope_kind": "account",
        "account_id": binding.account_id,
        "organization_id": None,
        "workspace_id": None,
    }


def make_challenge(event=None, **overrides) -> CTFChallenge:
    """Build an in-memory CTFChallenge without saving."""
    if event is None:
        event = make_ctf_event()
    defaults = {
        "id": uuid4(),
        "event": event,
        "name": "Test Challenge",
        "description": "Find the flag in the source code",
        "category": ChallengeCategory.WEB.value,
        "points": 100,
        "difficulty": ChallengeDifficulty.EASY.value,
        "flag_format": "FLAG{...}",
        "release_time": None,
        "order": 0,
    }
    defaults.update(overrides)
    return CTFChallenge(**defaults)


def make_bracket(event=None, **overrides) -> CTFBracket:
    """Build an in-memory CTFBracket without saving."""
    if event is None:
        event = make_ctf_event()
    defaults = {
        "id": uuid4(),
        "event": event,
        "name": "Test Bracket",
        "description": "",
        "display_order": 0,
    }
    defaults.update(overrides)
    return CTFBracket(**defaults)


def make_team(event=None, **overrides) -> CTFTeam:
    """Build an in-memory CTFTeam without saving."""
    if event is None:
        event = make_ctf_event(team_mode=True, team_size_limit=4)
    defaults = {
        "id": uuid4(),
        "event": event,
        "name": "Test Team",
        "invite_code": "test-invite-code-12345678",
    }
    defaults.update(overrides)
    return CTFTeam(**defaults)


def make_participant(event=None, **overrides) -> CTFParticipant:
    """Build an in-memory CTFParticipant without saving.

    Uses `user_id` to avoid Django FK descriptor validation.
    """
    if event is None:
        event = make_ctf_event()
    defaults = {
        "id": uuid4(),
        "event": event,
        "email": "participant@test.com",
        "name": "Test Participant",
        "user_id": 1,
        "status": ParticipantStatus.ACTIVE.value,
        "registered_at": timezone.now(),
        "last_active_at": None,
    }
    defaults.update(overrides)
    return CTFParticipant(**defaults)


def make_scheduled_task(event=None, **overrides) -> CTFScheduledTask:
    """Build an in-memory CTFScheduledTask without saving."""
    if event is None:
        event = make_ctf_event()
    defaults = {
        "id": uuid4(),
        "event": event,
        "task_type": "spin_up_ranges",
        "scheduled_for": timezone.now() + timedelta(hours=1),
        "status": ScheduledTaskStatus.PENDING.value,
        "error_message": "",
        "executed_at": None,
    }
    defaults.update(overrides)
    return CTFScheduledTask(**defaults)


# -----------------------------------------------------------------------------
# User Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def organizer_user(db) -> User:
    """Create a CTF organizer user with profile and group."""
    from management.services import get_user_profile

    user = User.objects.create_user(
        username="organizer@test.com",
        email="organizer@test.com",
        password="testpass123",  # nosec B106
        first_name="Test",
        last_name="Organizer",
    )
    group, _ = Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_organizer"
    profile.save(update_fields=["user_type"])
    individual_event_scope(user)
    return user


@pytest.fixture
def participant_user(db) -> User:
    """Create a CTF participant user with profile and group."""
    from management.services import get_user_profile

    user = User.objects.create_user(
        username="participant@test.com",
        email="participant@test.com",
        password="testpass123",  # nosec B106
        first_name="Test",
        last_name="Participant",
    )
    group, _ = Group.objects.get_or_create(name=CTF_PARTICIPANT_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_participant"
    profile.save(update_fields=["user_type"])
    return user


@pytest.fixture
def second_participant_user(db) -> User:
    """Create a second CTF participant user with profile and group."""
    from management.services import get_user_profile

    user = User.objects.create_user(
        username="participant2@test.com",
        email="participant2@test.com",
        password="testpass123",  # nosec B106
        first_name="Second",
        last_name="Participant",
    )
    group, _ = Group.objects.get_or_create(name=CTF_PARTICIPANT_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_participant"
    profile.save(update_fields=["user_type"])
    return user


@pytest.fixture
def standard_user(db) -> User:
    """Create a standard (non-CTF) user."""
    user = User.objects.create_user(
        username="standard@test.com",
        email="standard@test.com",
        password="testpass123",  # nosec B106
        first_name="Standard",
        last_name="User",
    )
    # Standard users get default profile via get_user_profile if accessed
    return user


@pytest.fixture
def admin_user(db) -> User:
    """Create a superuser."""
    user = User.objects.create_superuser(
        username="admin@test.com",
        email="admin@test.com",
        password="adminpass123",  # nosec B106
    )
    return user


# -----------------------------------------------------------------------------
# Event Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def ctf_event(db, organizer_user) -> CTFEvent:
    """Create a scheduled CTF event."""
    return CTFEvent.objects.create(
        name="Test CTF Event",
        description="A test CTF event for unit testing",
        created_by=organizer_user,
        **individual_event_scope(organizer_user),
        status=EventStatus.REGISTRATION.value,
        event_start=timezone.now() + timedelta(days=1),
        event_end=timezone.now() + timedelta(days=1, hours=8),
        scenario_id="basic",
        auto_cleanup=True,
        cleanup_delay_hours=24,
        team_mode=False,
        participant_password_override=TEST_CTF_BOOTSTRAP_PASSWORD,
    )


@pytest.fixture
def ctf_event_draft(db, organizer_user) -> CTFEvent:
    """Create a draft CTF event."""
    return CTFEvent.objects.create(
        name="Draft CTF Event",
        description="A draft event",
        created_by=organizer_user,
        **individual_event_scope(organizer_user),
        status=EventStatus.DRAFT.value,
        event_start=timezone.now() + timedelta(days=7),
        event_end=timezone.now() + timedelta(days=7, hours=8),
        scenario_id="basic",
        participant_password_override=TEST_CTF_BOOTSTRAP_PASSWORD,
    )


@pytest.fixture
def ctf_event_active(db, organizer_user) -> CTFEvent:
    """Create an active CTF event."""
    return CTFEvent.objects.create(
        name="Active CTF Event",
        description="An active event",
        created_by=organizer_user,
        **individual_event_scope(organizer_user),
        status=EventStatus.ACTIVE.value,
        event_start=timezone.now() - timedelta(hours=1),
        event_end=timezone.now() + timedelta(hours=7),
        scenario_id="basic",
        participant_password_override=TEST_CTF_BOOTSTRAP_PASSWORD,
    )


@pytest.fixture
def ctf_event_team(db, organizer_user) -> CTFEvent:
    """Create a team-based CTF event."""
    return CTFEvent.objects.create(
        name="Team CTF Event",
        description="A team-based event",
        created_by=organizer_user,
        **individual_event_scope(organizer_user),
        status=EventStatus.REGISTRATION.value,
        event_start=timezone.now() + timedelta(days=1),
        event_end=timezone.now() + timedelta(days=1, hours=8),
        scenario_id="basic",
        team_mode=True,
        team_size_limit=4,
        participant_password_override=TEST_CTF_BOOTSTRAP_PASSWORD,
    )


# -----------------------------------------------------------------------------
# Challenge Fixtures
# -----------------------------------------------------------------------------


def _add_static_flag(challenge: CTFChallenge, flag: str = "FLAG{test}", *, order: int = 0) -> CTFFlag:
    """Attach one static CTFFlag to a challenge (CTFFlag is the flag source of
    truth; #532)."""

    return CTFFlag.objects.create(
        challenge=challenge,
        value=flag,
        flag_type="static",
        case_sensitive=True,
        order=order,
    )


@pytest.fixture
def ctf_challenge(db, ctf_event) -> CTFChallenge:
    """Create a basic challenge with one static flag (CTFFlag is the source of
    truth; #532)."""
    challenge = CTFChallenge.objects.create(
        event=ctf_event,
        name="Test Challenge",
        description="Find the flag in the source code",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
        flag_format="FLAG{...}",
    )
    _add_static_flag(challenge, "FLAG{test}")
    return challenge


@pytest.fixture
def ctf_challenge_with_hint(db, ctf_event) -> CTFChallenge:
    """Create a challenge with hint."""
    return CTFChallenge.objects.create(
        event=ctf_event,
        name="Challenge With Hint",
        description="A challenge with a hint",
        category=ChallengeCategory.CRYPTO.value,
        points=200,
        difficulty=ChallengeDifficulty.MEDIUM.value,
        hint="Look at the cipher mode",
        hint_penalty=25,
    )


@pytest.fixture
def ctf_challenge_delayed(db, ctf_event) -> CTFChallenge:
    """Create a challenge with delayed release."""
    return CTFChallenge.objects.create(
        event=ctf_event,
        name="Delayed Challenge",
        description="Released later in the event",
        category=ChallengeCategory.PWN.value,
        points=300,
        difficulty=ChallengeDifficulty.HARD.value,
        release_time=ctf_event.event_start + timedelta(hours=2),
    )


# -----------------------------------------------------------------------------
# Team Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def ctf_team(db, ctf_event_team) -> CTFTeam:
    """Create a team."""
    return CTFTeam.objects.create(
        event=ctf_event_team,
        name="Test Team",
    )


# -----------------------------------------------------------------------------
# Participant Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def ctf_participant(db, ctf_event, participant_user) -> CTFParticipant:
    """Create an active participant."""
    from shared.principal_port import principal_for_user

    return CTFParticipant.objects.create(
        event=ctf_event,
        user=participant_user,
        principal_uuid=principal_for_user(participant_user).uuid,
        email=participant_user.email,
        name="Test Participant",
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
    )


@pytest.fixture
def ctf_participant_no_account(db, ctf_event) -> CTFParticipant:
    """Create a participant row with no linked isolated account (``user`` is None)."""
    return CTFParticipant.objects.create(
        event=ctf_event,
        email="no-account@test.com",
        name="No Account Participant",
        status=ParticipantStatus.REGISTERED.value,
        login_info_sent_at=timezone.now(),
    )


@pytest.fixture
def ctf_participant_team(db, ctf_event_team, ctf_team, participant_user) -> CTFParticipant:
    """Create a participant in a team."""
    from shared.principal_port import principal_for_user

    return CTFParticipant.objects.create(
        event=ctf_event_team,
        user=participant_user,
        principal_uuid=principal_for_user(participant_user).uuid,
        email=participant_user.email,
        name="Team Participant",
        team=ctf_team,
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
    )


# -----------------------------------------------------------------------------
# Submission Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def ctf_submission_correct(db, ctf_participant, ctf_challenge) -> CTFSubmission:
    """Create a correct submission."""
    return CTFSubmission.objects.create(
        participant=ctf_participant,
        challenge=ctf_challenge,
        submitted_flag="FLAG{correct}",
        is_correct=True,
        points_awarded=ctf_challenge.points,
        attempt_number=1,
        ip_address="192.168.1.1",
    )


@pytest.fixture
def ctf_submission_incorrect(db, ctf_participant, ctf_challenge) -> CTFSubmission:
    """Create an incorrect submission."""
    return CTFSubmission.objects.create(
        participant=ctf_participant,
        challenge=ctf_challenge,
        submitted_flag="FLAG{wrong}",
        is_correct=False,
        points_awarded=0,
        attempt_number=1,
        ip_address="192.168.1.1",
    )


# -----------------------------------------------------------------------------
# Authenticated Client Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def authenticated_organizer_client(client: Client, organizer_user) -> Client:
    """Client logged in as organizer."""
    client.force_login(organizer_user)
    return client


@pytest.fixture
def authenticated_participant_client(client: Client, participant_user) -> Client:
    """Client logged in as participant."""
    client.force_login(participant_user)
    return client


@pytest.fixture
def authenticated_admin_client(client: Client, admin_user) -> Client:
    """Client logged in as admin."""
    client.force_login(admin_user)
    return client


@pytest.fixture
def authenticated_standard_client(client: Client, standard_user) -> Client:
    """Client logged in as standard user."""
    client.force_login(standard_user)
    return client


@pytest.fixture
def second_organizer_user(db) -> User:
    """Create a second CTF organizer user with profile and group."""
    from management.services import get_user_profile

    user = User.objects.create_user(
        username="organizer2@test.com",
        email="organizer2@test.com",
        password="testpass123",  # nosec B106
        first_name="Second",
        last_name="Organizer",
    )
    group, _ = Group.objects.get_or_create(name=CTF_ORGANIZER_GROUP)
    user.groups.add(group)
    profile = get_user_profile(user)
    profile.user_type = "ctf_organizer"
    profile.save(update_fields=["user_type"])
    return user


@pytest.fixture
def request_factory():
    """Provide Django RequestFactory."""
    from django.test import RequestFactory

    return RequestFactory()


# -----------------------------------------------------------------------------
# Mock Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def mock_range_service(mocker) -> MagicMock:
    """Mock the CMS range creation service."""
    mock = mocker.patch(
        "cms.services.create_range",
        return_value=MagicMock(id=1, status="provisioning"),
    )
    return mock


@pytest.fixture
def mock_email_backend(mocker) -> MagicMock:
    """Mock email sending."""
    mock = mocker.patch("django.core.mail.send_mail", return_value=1)
    return mock


@pytest.fixture(autouse=True)
def isolate_ctf_rate_counters():
    """Database rollbacks reuse actor IDs; rate budgets must not span tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
