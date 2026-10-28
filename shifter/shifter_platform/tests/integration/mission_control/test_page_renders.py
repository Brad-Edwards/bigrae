"""Authenticated page-render query-budget tests (#924, TEST-7).

These render the authenticated pages that carry the event load through the real
Django request/response stack with ``django.test.Client`` and count the actual
SQL queries with ``CaptureQueriesContext``, with no ``render`` patching. Each
budget is an exact, named integer: a regression that adds a query fails the
assertion and must be justified by bumping the number deliberately.

Every page here is the SPA shell. Its render runs no project context processor,
so an active range or an active CTF event adds no query (#2593).
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

User = get_user_model()

# Exact per-page query budget. Bump deliberately (with justification) when a
# real change adds queries; an accidental regression must fail here first.
#
# The SPA shell render costs the session, the authenticated user, and the live
# principal-lifecycle lookup (#2316) that stops a deactivated or retired
# identity from keeping a session. #2593 removed the legacy template context
# processors that added range, permission and CTF navigation queries.
SPA_SHELL_BUDGET = 3


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="render-user@example.com",
        email="render-user@example.com",
        password="render-pass-123",
    )


@pytest.fixture
def client_for(db):
    """Factory: a logged-in client (with OIDC session) for a given user."""

    def _client(user) -> Client:
        client = Client()
        client.force_login(user)
        session = client.session
        session["oidc_id_token_expiration"] = time.time() + 3600
        session.save()
        return client

    return _client


@pytest.fixture
def active_range(db, user):
    """Seed a non-deleted CMS range so ``get_active_range`` returns a context."""
    from cms.models import RangeInstance
    from cms.models import Request as CMSRequest
    from shared.enums import RequestType
    from workspaces.services import resolve_personal_workspace

    workspace_id = resolve_personal_workspace(user).workspace_id

    request = CMSRequest.objects.create(
        workspace_id=workspace_id,
        request_id=uuid.uuid4(),
        request_type=RequestType.RANGE.value,
        user=user,
    )
    return RangeInstance.objects.create(
        workspace_id=workspace_id,
        request=request,
        scenario_id="test_scenario",
        user_id=user.id,
        status="ready",
    )


@pytest.fixture
def ctf_participant(db, user):
    """Make ``user`` an active CTF participant with an active event selected."""
    from ctf.enums import ParticipantStatus
    from ctf.models import CTFEvent, CTFParticipant
    from management.services import set_active_ctf_event
    from shared.auth import CTF_PARTICIPANT_GROUP

    event = CTFEvent.objects.create(
        name="Render Test Event",
        created_by=user,
        event_start=timezone.now() - timedelta(hours=1),
        event_end=timezone.now() + timedelta(hours=2),
    )
    participant = CTFParticipant.objects.create(
        event=event,
        user=user,
        email=user.email,
        name="Render Participant",
        status=ParticipantStatus.ACTIVE.value,
        registered_at=timezone.now(),
    )
    group, _ = Group.objects.get_or_create(name=CTF_PARTICIPANT_GROUP)
    user.groups.add(group)
    set_active_ctf_event(user, event.id)
    return participant


def _render_query_count(client: Client, url: str):
    """Return (response, query_count) for a GET, capturing only the render."""
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)
    return response, len(ctx.captured_queries)


@pytest.mark.django_db
class TestPageRenderQueryBudgets:
    """Numeric per-render query budgets for authenticated pages."""

    def test_dashboard_no_active_range_budget(self, user, client_for):
        client = client_for(user)
        response, queries = _render_query_count(client, "/mission-control/")

        assert response.status_code == 200
        assert queries == SPA_SHELL_BUDGET

    def test_dashboard_active_range_budget(self, user, client_for, active_range):
        client = client_for(user)
        response, queries = _render_query_count(client, "/mission-control/")

        assert response.status_code == 200
        assert queries == SPA_SHELL_BUDGET

    def test_terminal_budget(self, user, client_for):
        client = client_for(user)
        response, queries = _render_query_count(client, "/mission-control/terminal/")

        assert response.status_code == 200
        assert queries == SPA_SHELL_BUDGET

    def test_terminal_active_range_budget(self, user, client_for, active_range):
        client = client_for(user)
        response, queries = _render_query_count(client, "/mission-control/terminal/")

        assert response.status_code == 200
        assert queries == SPA_SHELL_BUDGET

    def test_ctf_participant_dashboard_budget(self, user, client_for, ctf_participant):
        client = client_for(user)
        response, queries = _render_query_count(client, "/ctf/")

        assert response.status_code == 200
        assert queries == SPA_SHELL_BUDGET
