"""Tests for shared DRF API permissions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from rest_framework.test import APIRequestFactory

from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api_tokens import scopes
from shared.api_tokens.models import ApiToken
from shared.api_tokens.permissions import require_scope
from shared.authorization import CredentialCeiling
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef

pytestmark = pytest.mark.django_db


def test_schema_permission_allows_authenticated_session_user(django_user_model) -> None:
    user = django_user_model.objects.create_user(username="session", password="pw")
    request = APIRequestFactory().get("/api/v1/schema/")
    request.user = user
    request.auth = None

    assert IsAuthenticatedSessionOrApiToken().has_permission(request, view=None) is True


def test_schema_permission_allows_valid_platform_api_token(django_user_model) -> None:
    owner = django_user_model.objects.create_user(username="owner", password="pw")
    token, _ = ApiToken.create_token(name="schema", created_by=owner, scopes=[scopes.MISSION_CONTROL_RANGE_READ])
    request = APIRequestFactory().get("/api/v1/schema/")
    request.user = AnonymousUser()
    request.auth = token

    assert IsAuthenticatedSessionOrApiToken().has_permission(request, view=None) is True


def test_schema_permission_rejects_anonymous_request() -> None:
    request = APIRequestFactory().get("/api/v1/schema/")
    request.user = AnonymousUser()
    request.auth = None

    assert IsAuthenticatedSessionOrApiToken().has_permission(request, view=None) is False


def test_scope_permission_enforces_native_service_credential_scopes() -> None:
    permission = require_scope(scopes.CTF_COMMUNICATION_READ, scopes.CTF_COMMUNICATION_WRITE)()
    request = APIRequestFactory().post("/api/v1/ctf/communications/", {})
    request.auth = CredentialContext(
        principal=PrincipalRef(uuid4(), "service"),
        kind="service",
        credential_uuid=uuid4(),
        ceiling=CredentialCeiling(frozenset({"event.manage_communications"})),
        scopes=frozenset({scopes.CTF_COMMUNICATION_READ}),
    )

    assert permission.has_permission(request, view=None) is False

    request.auth = CredentialContext(
        principal=request.auth.principal,
        kind="service",
        credential_uuid=uuid4(),
        ceiling=request.auth.ceiling,
        scopes=frozenset({scopes.CTF_COMMUNICATION_WRITE}),
    )
    assert permission.has_permission(request, view=None) is True
