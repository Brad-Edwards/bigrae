"""Behavior tests for ApiToken DRF authentication and the audit seam (PLAT-102).

Covers the fail-closed semantics required by the preflight: a *bad* bearer token
is an authentication failure and must never fall through to a session, while
*no* bearer credential falls back to session auth. The audit seam is exercised
both as a monkeypatched spy (auth path) and end-to-end against a real AuditLog
row (seam path).
"""

from __future__ import annotations

import pytest
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory

from shared.api_tokens import audit, scopes
from shared.api_tokens.authentication import ApiTokenAuthentication
from shared.api_tokens.models import ApiToken
from shared.audit import (
    AuditAction,
    AuditActorType,
)
from shared.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="tokuser", password="x")


@pytest.fixture
def factory():
    return APIRequestFactory()


def _auth(factory, header: str | None):
    extra = {"HTTP_AUTHORIZATION": header} if header is not None else {}
    request = factory.get("/api/v1/risks/", **extra)
    return ApiTokenAuthentication().authenticate(request)


class TestNoTokenFallsBackToSession:
    def test_absent_authorization_returns_none(self, factory):
        assert _auth(factory, None) is None

    def test_non_bearer_scheme_returns_none(self, factory):
        # An unsupported authentication scheme is not ours; defer.
        assert _auth(factory, "Basic Zm9vOmJhcg==") is None


@pytest.mark.usefixtures("personal_token_use_grant")
class TestValidToken:
    def test_authenticates_and_exposes_scopes(self, factory, user):
        token, raw = ApiToken.create_token(name="ci", created_by=user, scopes=[scopes.MISSION_CONTROL_RANGE_READ])
        result = _auth(factory, f"Bearer {raw}")
        assert result is not None
        auth_user, auth_token = result
        assert auth_user is None
        assert auth_token.pk == token.pk
        assert scopes.has_scope(auth_token.scopes, scopes.MISSION_CONTROL_RANGE_READ)

    def test_updates_last_used(self, factory, user):
        token, raw = ApiToken.create_token(name="ci", created_by=user, scopes=[scopes.MISSION_CONTROL_RANGE_READ])
        assert token.last_used_at is None
        _auth(factory, f"Bearer {raw}")
        token.refresh_from_db()
        assert token.last_used_at is not None


class TestBadTokenFailsClosed:
    def test_invalid_token_raises_and_audits(self, factory, user, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "shared.api_tokens.authentication.record_token_event",
            lambda *a, **k: calls.append((a, k)),
        )
        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, "Bearer shf_bogus.deadbeef")
        assert calls, "auth failure must be audited at the edge"
        # ...and specifically as an AUTH_FAILED event, not some other event type.
        event_arg = calls[0][0][0]
        assert event_arg == audit.TokenEvent.AUTH_FAILED

    def test_revoked_token_raises(self, factory, user):
        token, raw = ApiToken.create_token(name="ci", created_by=user, scopes=[scopes.MISSION_CONTROL_RANGE_READ])
        token.revoke()
        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, f"Bearer {raw}")

    def test_ctf_account_token_is_force_revoked_and_rejected(self, factory, django_user_model):
        """A token owned by a CTF-participant account is force-revoked and rejected.

        Fail-closed guard (shared/api_tokens/authentication.py): CTF/participant
        accounts must never authenticate via API tokens, and the attempt revokes
        the token as a side effect so a leaked token cannot be retried.
        """
        from management.services import get_user_profile

        owner = django_user_model.objects.create_user(username="ctfowner", password="x")
        profile = get_user_profile(owner)
        profile.user_type = "ctf_participant"
        profile.is_ctf_account = True
        profile.save()

        token, raw = ApiToken.create_token(name="ctf", created_by=owner, scopes=[scopes.MISSION_CONTROL_RANGE_READ])
        assert token.is_active is True

        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, f"Bearer {raw}")

        token.refresh_from_db()
        assert token.revoked_at is not None
        assert token.is_active is False

    def test_malformed_bearer_raises(self, factory):
        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, "Bearer")
        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, "Bearer one two")

    def test_authenticate_header_is_bearer(self, factory):
        request = factory.get("/api/v1/risks/")
        assert ApiTokenAuthentication().authenticate_header(request) == "Bearer"


class TestAuditSeam:
    def test_auth_failure_row_is_attributed_to_token_principal(self, factory):
        request = factory.get("/api/v1/risks/")
        before = AuditLog.objects.count()
        audit.record_token_event(audit.TokenEvent.AUTH_FAILED, request=request, context="bad token")
        assert AuditLog.objects.count() == before + 1
        row = AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED).latest("timestamp")
        # A failed auth is the token principal's, not a user's.
        assert row.actor_type == AuditActorType.APIKEY
        assert row.actor_id is None

    def test_create_and_revoke_rows_are_attributed_to_acting_user(self):
        # Admin create/revoke pass the acting staff user's id; the row must read
        # as a USER action, not an API-key action.
        audit.record_token_event(audit.TokenEvent.CREATED, token_id="abc", token_pk=1, actor_id=7)
        audit.record_token_event(audit.TokenEvent.REVOKED, token_id="abc", token_pk=1, actor_id=7)
        created = AuditLog.objects.get(action=AuditAction.CREATE)
        revoked = AuditLog.objects.get(action=AuditAction.DELETE)
        assert created.actor_type == AuditActorType.USER
        assert created.actor_id == 7
        assert revoked.actor_type == AuditActorType.USER
        assert revoked.actor_id == 7


def _failed_token_rows():
    return AuditLog.objects.filter(action=AuditAction.LOGIN_FAILED, actor_type=AuditActorType.APIKEY)


class TestEveryRejectionIsAudited:
    """Every bearer rejection writes one AUTH_FAILED row and no credential material (#2617)."""

    @pytest.mark.parametrize("header", ["Bearer", "Bearer one two", "Bearer not.a.credential"])
    def test_malformed_header_and_service_credential_rejections_are_audited(self, factory, header):
        before = _failed_token_rows().count()

        with pytest.raises(exceptions.AuthenticationFailed):
            _auth(factory, header)

        assert _failed_token_rows().count() == before + 1

    def test_owner_and_policy_rejections_name_the_token_but_never_its_secret(self, factory, user, django_user_model):
        from management.services import get_user_profile

        no_grant, no_grant_raw = ApiToken.create_token(
            name="no-grant", created_by=user, scopes=[scopes.MISSION_CONTROL_RANGE_READ]
        )
        deleted_owner = django_user_model.objects.create_user(username="deleted-owner", password="x")
        deleted, deleted_raw = ApiToken.create_token(
            name="deleted", created_by=deleted_owner, scopes=[scopes.MISSION_CONTROL_RANGE_READ]
        )
        deleted_profile = get_user_profile(deleted_owner)
        deleted_profile.deleted_at = timezone.now()
        deleted_profile.save()
        ctf_owner = django_user_model.objects.create_user(username="ctf-owner", password="x")
        profile = get_user_profile(ctf_owner)
        profile.user_type = "ctf_participant"
        profile.is_ctf_account = True
        profile.save()
        ctf_token, ctf_raw = ApiToken.create_token(
            name="ctf", created_by=ctf_owner, scopes=[scopes.MISSION_CONTROL_RANGE_READ]
        )

        # No personal-token grant: the live token-use policy denies the owner.
        for token, raw, reason in (
            (no_grant, no_grant_raw, "Token use is not authorized"),
            (deleted, deleted_raw, "Token owner is not eligible"),
            (ctf_token, ctf_raw, "Token owner is a CTF account; token revoked"),
        ):
            with pytest.raises(exceptions.AuthenticationFailed):
                _auth(factory, f"Bearer {raw}")
            row = _failed_token_rows().latest("timestamp")
            assert row.entity_id == token.pk
            assert row.new_state == {"token_id": token.token_id}
            assert row.context == reason
            assert raw not in str(row.new_state) + row.context

    def test_over_long_request_id_does_not_suppress_the_audit_row(self, factory):
        request = factory.get("/api/v1/risks/", HTTP_AUTHORIZATION="Bearer shf_bogus.deadbeef")
        request.META["HTTP_X_REQUEST_ID"] = "r" * 200
        before = _failed_token_rows().count()

        with pytest.raises(exceptions.AuthenticationFailed):
            ApiTokenAuthentication().authenticate(request)

        assert _failed_token_rows().count() == before + 1
        assert _failed_token_rows().latest("timestamp").request_id != "r" * 200


class TestRequestIdIngress:
    """RequestIDMiddleware keeps a bounded client correlation id and replaces any other value (#2617)."""

    @pytest.mark.parametrize(
        ("header", "kept"),
        [
            ("req-0123456789abcdef0123456789abcdef", True),
            ("trace:abc.def_1-2", True),
            ("r" * 65, False),
            ("bad id with spaces", False),
            ("<script>", False),
        ],
    )
    def test_client_request_id_is_bounded(self, rf, header, kept):
        from django.http import HttpResponse

        from config.middleware import RequestIDMiddleware

        request = rf.get("/", HTTP_X_REQUEST_ID=header)
        response = RequestIDMiddleware(lambda _request: HttpResponse())(request)

        assert (request.request_id == header) is kept
        assert len(request.request_id) <= 64
        assert response["X-Request-ID"] == request.request_id
