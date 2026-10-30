"""Tests for shared.email — the platform email templating and transport seam (ADR-071).

Delivery runs through Django's real ``EmailMultiAlternatives`` and connection
machinery. Only the external mail transport is replaced: the locmem backend, or
a fake backend whose ``send_messages`` raises a real provider exception class
(ADR-019-R1).
"""

from __future__ import annotations

import logging
import smtplib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from anymail.exceptions import AnymailAPIError, AnymailInvalidAddress, AnymailRecipientsRefused
from botocore.exceptions import ClientError, EndpointConnectionError
from django.core.mail.backends.base import BaseEmailBackend

from shared import email

FAKE_BACKEND = "tests.shared.test_email.FakeBackend"
RECIPIENT = "person@example.test"


class FakeBackend(BaseEmailBackend):
    """External-transport stand-in configured per test through class attributes."""

    error: BaseException | None = None
    sent = 1
    receipt: object = None
    options: dict[str, object] = {}

    def __init__(self, fail_silently: bool = False, **kwargs: object) -> None:
        super().__init__(fail_silently=fail_silently)
        FakeBackend.options = kwargs

    def send_messages(self, email_messages):
        if FakeBackend.error is not None:
            raise FakeBackend.error
        for message in email_messages:
            message.message()  # Build the MIME message exactly as a real backend does.
            if FakeBackend.receipt is not None:
                message.anymail_status = SimpleNamespace(message_id=FakeBackend.receipt)
        return FakeBackend.sent


@pytest.fixture
def fake_backend(settings):
    """Select the fake transport and reset its per-test behaviour."""
    settings.EMAIL_BACKEND = FAKE_BACKEND
    FakeBackend.error = None
    FakeBackend.sent = 1
    FakeBackend.receipt = None
    FakeBackend.options = {}
    return FakeBackend


def _attempt(**overrides):
    kwargs = {"timeout": 10, "message_key": "intent:participant:email"}
    kwargs.update(overrides)
    return email.attempt_email(RECIPIENT, "Subject", "<p>Body</p>", "Body", **kwargs)


class TestRenderTemplate:
    """Tests for render_template()."""

    @patch("django.template.loader.render_to_string")
    def test_renders_html_and_text(self, mock_render):
        """Renders both .html and .txt templates."""
        mock_render.side_effect = ["<html>Hello</html>", "Hello"]

        html, text = email.render_template("ctf/email/notice/reminder", {"key": "val"})

        assert (html, text) == ("<html>Hello</html>", "Hello")
        mock_render.assert_any_call("ctf/email/notice/reminder.html", {"key": "val"})
        mock_render.assert_any_call("ctf/email/notice/reminder.txt", {"key": "val"})


class TestAttemptEmail:
    """One bounded, classified send attempt."""

    def test_accepted_message_reaches_the_transport_once_with_a_stable_message_id(self, mailoutbox, settings):
        settings.DEFAULT_FROM_EMAIL = "Platform <noreply@mail.example.test>"

        first = _attempt()
        second = _attempt()

        assert first.outcome == email.EmailOutcome.ACCEPTED
        assert first.reason == "backend_accepted"
        assert len(mailoutbox) == 2
        message = mailoutbox[0]
        assert message.to == [RECIPIENT]
        assert message.cc == [] and message.bcc == []
        assert message.alternatives[0][0] == "<p>Body</p>"
        message_id = message.extra_headers["Message-ID"]
        assert message_id.startswith("<") and message_id.endswith("@mail.example.test>")
        assert mailoutbox[1].extra_headers["Message-ID"] == message_id
        assert second.outcome == email.EmailOutcome.ACCEPTED

    def test_distinct_keys_get_distinct_message_ids_and_no_key_gets_no_header(self, mailoutbox):
        _attempt(message_key="a")
        _attempt(message_key="b")
        _attempt(message_key="")

        ids = [message.extra_headers.get("Message-ID") for message in mailoutbox]
        assert ids[0] != ids[1]
        assert ids[2] is None

    def test_unavailable_backend_sends_nothing(self, mailoutbox, settings):
        settings.EMAIL_DELIVERY_AVAILABLE = False

        result = _attempt()

        assert result.outcome == email.EmailOutcome.UNAVAILABLE
        assert result.reason == "transport_unavailable"
        assert mailoutbox == []
        assert email.delivery_available() is False

    def test_zero_sent_is_not_acceptance(self, fake_backend):
        fake_backend.sent = 0

        result = _attempt()

        assert (result.outcome, result.reason) == (email.EmailOutcome.RETRIABLE, "not_accepted")

    @pytest.mark.parametrize(
        ("receipt", "expected"),
        [
            ("<0100018b.abc@email.amazonses.com>", "<0100018b.abc@email.amazonses.com>"),
            ("bad\r\nreceipt", ""),
            ("x" * 256, ""),
            ({"a", "b"}, ""),
        ],
    )
    def test_receipt_is_kept_only_when_bounded_and_printable(self, fake_backend, receipt, expected):
        fake_backend.receipt = receipt

        result = _attempt()

        assert result.outcome == email.EmailOutcome.ACCEPTED
        assert result.receipt == expected

    @pytest.mark.parametrize(
        ("error", "outcome", "reason"),
        [
            (AnymailRecipientsRefused("refused"), "terminal", "recipient_rejected"),
            (AnymailInvalidAddress("bad address"), "terminal", "invalid_message"),
            (AnymailAPIError("bad request", status_code=400), "terminal", "provider_rejected"),
            (AnymailAPIError("bad key", status_code=401), "retriable", "transport_unavailable"),
            (AnymailAPIError("slow down", status_code=429), "retriable", "provider_throttled"),
            (AnymailAPIError("down", status_code=503), "retriable", "provider_unavailable"),
            (AnymailAPIError("no response"), "retriable", "provider_unavailable"),
            (smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"no")}), "terminal", "recipient_rejected"),
            (smtplib.SMTPDataError(554, b"content"), "terminal", "provider_rejected"),
            (smtplib.SMTPDataError(451, b"later"), "retriable", "provider_unavailable"),
            (smtplib.SMTPSenderRefused(550, b"sender", "noreply@x"), "retriable", "transport_unavailable"),
            (smtplib.SMTPAuthenticationError(535, b"auth"), "retriable", "transport_unavailable"),
            (smtplib.SMTPConnectError(554, b"go away"), "retriable", "provider_unavailable"),
            (smtplib.SMTPServerDisconnected("gone"), "retriable", "provider_unavailable"),
            (TimeoutError("timed out"), "retriable", "provider_unavailable"),
            (
                ClientError({"Error": {"Code": "MessageRejected", "Message": "x"}}, "SendRawEmail"),
                "terminal",
                "provider_rejected",
            ),
            (
                ClientError({"Error": {"Code": "Throttling", "Message": "x"}}, "SendRawEmail"),
                "retriable",
                "provider_throttled",
            ),
            (
                ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "SendRawEmail"),
                "retriable",
                "provider_unavailable",
            ),
            (EndpointConnectionError(endpoint_url="https://email.example"), "retriable", "provider_unavailable"),
            (ValueError("Header values may not contain linefeed"), "terminal", "invalid_message"),
            (RuntimeError("unexpected"), "retriable", "transport_error"),
        ],
    )
    def test_backend_errors_are_classified_into_closed_outcomes(self, fake_backend, error, outcome, reason):
        fake_backend.error = error

        result = _attempt()

        assert (result.outcome.value, result.reason) == (outcome, reason)
        assert result.reason in email.REASONS

    def test_failure_log_carries_no_recipient_body_or_provider_text(self, fake_backend, caplog):
        fake_backend.error = AnymailAPIError(f"rejected {RECIPIENT} secret-provider-text", status_code=400)

        with caplog.at_level(logging.DEBUG, logger="shared.email"):
            _attempt()

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "provider_rejected" in logged
        assert RECIPIENT not in logged
        assert "secret-provider-text" not in logged
        assert all(record.exc_info is None for record in caplog.records)


class TestConnectionOptions:
    """The timeout is applied inside each supported backend."""

    def test_smtp_takes_its_limits_from_the_deadline_not_options(self):
        # The SMTP connection class re-arms each operation from the attempt deadline.
        assert email.connection_options("django.core.mail.backends.smtp.EmailBackend", 10) == {}

    @pytest.mark.parametrize(
        "backend", ["anymail.backends.sendgrid.EmailBackend", "anymail.backends.mailgun.EmailBackend"]
    )
    def test_anymail_splits_the_budget_between_connect_and_read(self, backend):
        assert email.connection_options(backend, 10) == {"requests_timeout": (5.0, 5.0)}

    def test_ses_bounds_timeouts_and_disables_sdk_retries(self):
        options = email.connection_options("django_ses.SESBackend", 10)

        config = options["aws_config"]
        assert config.connect_timeout == 5.0
        assert config.read_timeout == 5.0
        assert config.retries == {"total_max_attempts": 1}

    def test_other_backends_get_no_options(self):
        assert email.connection_options("django.core.mail.backends.locmem.EmailBackend", 10) == {}

    def test_attempt_passes_the_options_to_the_selected_backend(self, fake_backend, monkeypatch):
        monkeypatch.setattr(email, "connection_options", lambda backend, timeout: {"marker": timeout})

        _attempt(timeout=7)

        assert fake_backend.options == {"marker": 7}


def test_temporary_recipient_refusal_is_retriable(fake_backend):
    fake_backend.error = smtplib.SMTPRecipientsRefused({RECIPIENT: (451, b"greylisted, try later")})

    result = _attempt()

    assert (result.outcome, result.reason) == (email.EmailOutcome.RETRIABLE, "recipient_deferred")


def test_mixed_refusal_is_terminal_only_when_every_code_is_permanent(fake_backend):
    fake_backend.error = smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"no"), "other@example.test": (450, b"busy")})

    assert _attempt().outcome == email.EmailOutcome.RETRIABLE
