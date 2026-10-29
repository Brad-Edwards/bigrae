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


@pytest.mark.parametrize(
    ("code", "status"),
    [("reset_delivery_failed", 503), ("reset_throttled", 409), ("reset_delivery_unavailable", 400)],
)
def test_policy_api_maps_reset_errors_to_http_status(code, status):
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from management.api.policy_views import PolicyAdminUserResetPasswordView

    view = PolicyAdminUserResetPasswordView()
    view.request = Request(APIRequestFactory().post("/api/v1/administer/users/1/reset-password/"))
    view.format_kwarg = None

    response = view.handle_exception(password_reset.PasswordResetError(code, "Bounded message"))

    assert response.status_code == status
    assert response.data["error"]["code"] == code
