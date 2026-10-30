"""Behavior tests for the unified logout view (config/views.py:logout_view).

Drives the real view through the test Client with a real session: the real
Django ``logout`` flushes the session and the view redirects appropriately,
instead of patching ``config.views.logout`` and asserting it was called.
"""

import logging

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from shared.audit import (
    AuditAction,
    AuditEntityType,
)
from shared.models import AuditLog

pytestmark = pytest.mark.django_db

User = get_user_model()

LOGOUT_URL = "/logout/"


COGNITO_ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_Synthetic1"


@pytest.fixture
def user(db):
    return User.objects.create_user(username="logout@example.com", email="logout@example.com")


@pytest.fixture
def oidc_login(settings):
    """Sign in through the Cognito backend with the assurance a real login records."""
    import time

    from config.session_credentials import establish_identity_session
    from management.services import bind_provider_identity

    settings.OIDC_ISSUER_URL = COGNITO_ISSUER

    def login(client, user):
        bind_provider_identity(user, COGNITO_ISSUER, f"subject-{user.pk}")
        client.force_login(user, backend="config.oidc.ShifterOIDCBackend")
        session = client.session
        establish_identity_session(
            session, {"iss": COGNITO_ISSUER, "sub": f"subject-{user.pk}", "auth_time": int(time.time())}
        )
        session.save()

    return login


class TestLogoutView:
    def test_non_oidc_user_gets_session_logout(self, user):
        """A ModelBackend (non-OIDC) user is logged out and sent to the landing page."""
        client = Client()
        client.force_login(user, backend="config.auth.PlatformModelBackend")
        assert "_auth_user_id" in client.session  # logged in

        response = client.post(LOGOUT_URL)

        assert response.status_code == 302
        assert response.url == "/"
        # Real logout flushed the session.
        assert "_auth_user_id" not in client.session

    def test_oidc_user_redirects_to_landing_without_op_logout(self, user, oidc_login):
        """An OIDC-backend user is logged out; with no Cognito logout method
        configured in test settings, the view falls back to the landing page."""
        client = Client()
        oidc_login(client, user)

        response = client.post(LOGOUT_URL)

        assert response.status_code == 302
        assert response.url == "/"
        assert "_auth_user_id" not in client.session

    @override_settings(OIDC_OP_LOGOUT_URL_METHOD="config.oidc.provider_logout_url")
    def test_oidc_user_redirects_to_provider_logout_url(self, user, monkeypatch, oidc_login):
        """With OIDC_OP_LOGOUT_URL_METHOD set, a user signed in through the
        deployed ``config.oidc.ShifterOIDCBackend`` is redirected to the identity
        provider's logout endpoint (not the local landing page).

        Uses the real backend path: its dotted path does not contain the
        ``OIDCAuthenticationBackend`` base-class name, and a substring-based
        branch skipped the provider logout and left the IdP session alive.
        Also exercises the real ``config.oidc.provider_logout_url`` builder.
        """
        monkeypatch.setenv("OIDC_AUTH_DOMAIN", "auth.example.com")
        monkeypatch.setenv("OIDC_RP_CLIENT_ID", "client123")

        client = Client()
        oidc_login(client, user)

        response = client.post(LOGOUT_URL)

        assert response.status_code == 302
        assert "auth.example.com/logout" in response.url
        assert "client_id=client123" in response.url
        assert "_auth_user_id" not in client.session

    def test_unauthenticated_redirects_to_landing(self):
        """An unauthenticated POST redirects to the landing page."""
        response = Client().post(LOGOUT_URL)

        assert response.status_code == 302
        assert response.url == "/"

    def test_get_not_allowed(self, user):
        """GET is rejected (logout is POST-only)."""
        client = Client()
        client.force_login(user)

        response = client.get(LOGOUT_URL)

        assert response.status_code == 405

    def test_logout_writes_audit_row_with_email(self, user, oidc_login):
        """A successful logout writes a LOGOUT audit row identifying the user.

        The identity is captured before Django ``logout`` flushes the session.
        """
        client = Client()
        oidc_login(client, user)

        client.post(LOGOUT_URL)

        row = AuditLog.objects.get(action=AuditAction.LOGOUT, entity_type=AuditEntityType.USER)
        assert row.new_state["email"] == user.email

    def test_unauthenticated_logout_writes_no_audit_row(self):
        """An unauthenticated POST has no principal to audit."""
        Client().post(LOGOUT_URL)

        assert not AuditLog.objects.filter(action=AuditAction.LOGOUT).exists()

    def test_logout_debug_log_does_not_retain_email(self, user, caplog, oidc_login):
        client = Client()
        oidc_login(client, user)
        view_logger = logging.getLogger("config.views")
        with caplog.at_level(logging.DEBUG, logger=view_logger.name):
            client.post(LOGOUT_URL)

        assert f"logout for user_id={user.pk}" in caplog.text
        assert user.email not in caplog.text

    def test_dashboard_debug_log_does_not_retain_email(self, user, caplog, oidc_login):
        client = Client()
        oidc_login(client, user)
        view_logger = logging.getLogger("config.views")
        with caplog.at_level(logging.DEBUG, logger=view_logger.name):
            response = client.get("/dashboard/")

        assert response.status_code == 302
        assert f"Routing user_id={user.pk} " in caplog.text
        assert user.email not in caplog.text
