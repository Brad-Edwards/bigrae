"""Administrator password-reset delivery reports its observed outcome (ADR-071, #1525).

Transactional tests, so the reset's ``on_commit`` send runs when the service
transaction commits, exactly as in a request. Only the external mail transport
is replaced (ADR-019-R1).
"""

from __future__ import annotations

import smtplib

import pytest
from django.contrib.auth import get_user_model
from django.core.mail.backends.base import BaseEmailBackend
from rest_framework.test import APIClient

from management import password_reset
from management.services import AuditContext
from shared.models import AuditLog

pytestmark = pytest.mark.django_db(transaction=True)

User = get_user_model()
AUDIT_CONTEXT = "administrator password reset requested"


class RefusingBackend(BaseEmailBackend):
    """External SMTP stand-in that refuses the recipient."""

    def send_messages(self, email_messages):
        recipient = email_messages[0].to[0]
        raise smtplib.SMTPRecipientsRefused({recipient: (550, b"mailbox unavailable")})


@pytest.fixture
def reset_site(settings):
    settings.SITE_URL = "https://portal.example.com"
    settings.DEBUG = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    return settings


@pytest.fixture
def local_user():
    return User.objects.create_user(username="reset-subject", email="reset-subject@example.com", password="pw")


def _request(user):
    password_reset.request_password_reset(user, audit=AuditContext("user", None, request_id="reset-delivery"))


def test_accepted_reset_sends_one_message_to_the_resolved_user(reset_site, local_user, mailoutbox):
    _request(local_user)

    assert [message.to for message in mailoutbox] == [[local_user.email]]
    assert "/password-reset/" in mailoutbox[0].body or "reset" in mailoutbox[0].subject.lower()


def test_refused_reset_reports_failure_and_keeps_the_committed_audit(reset_site, local_user):
    reset_site.EMAIL_BACKEND = "tests.management.test_password_reset_delivery.RefusingBackend"

    with pytest.raises(password_reset.PasswordResetError) as caught:
        _request(local_user)

    assert caught.value.code == "reset_delivery_failed"
    assert AuditLog.objects.filter(context=AUDIT_CONTEXT, entity_id=local_user.id).exists()


def test_unavailable_transport_refuses_before_audit_and_sends_nothing(reset_site, local_user, mailoutbox):
    reset_site.EMAIL_DELIVERY_AVAILABLE = False

    with pytest.raises(password_reset.PasswordResetError) as caught:
        _request(local_user)

    assert caught.value.code == "reset_delivery_unavailable"
    assert mailoutbox == []
    assert not AuditLog.objects.filter(context=AUDIT_CONTEXT, entity_id=local_user.id).exists()


def test_admin_api_returns_service_unavailable_for_a_refused_send(reset_site, local_user):
    reset_site.EMAIL_BACKEND = "tests.management.test_password_reset_delivery.RefusingBackend"
    admin = User.objects.create_superuser(username="reset-root", email="root@example.com", password="pw")
    client = APIClient()
    client.force_authenticate(user=admin)

    response = client.post(f"/api/v1/administer/users/{local_user.id}/reset-password/", format="json")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "reset_delivery_failed"
    assert local_user.email not in response.content.decode()


def test_policy_api_returns_service_unavailable_for_a_refused_send(reset_site, local_user, monkeypatch):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from management.api.policy_views import PolicyAdminUserResetPasswordView
    from tests.management.test_policy_lifecycle import Provider, _service_actor

    reset_site.EMAIL_BACKEND = "tests.management.test_password_reset_delivery.RefusingBackend"
    actor = _service_actor()
    monkeypatch.setattr("management.api.policy_views.configured_authorization_provider", Provider)
    request = APIRequestFactory().post(f"/api/v1/administer/users/{local_user.pk}/reset-password/", {}, format="json")
    request.credential_context = actor
    force_authenticate(request, token=actor)

    response = PolicyAdminUserResetPasswordView.as_view()(request, pk=local_user.pk)

    assert response.status_code == 503
    assert response.data["error"]["code"] == "reset_delivery_failed"
