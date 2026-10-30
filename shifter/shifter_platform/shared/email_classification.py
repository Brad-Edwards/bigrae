"""Closed classification of email transport results (ADR-071, #1525).

Maps a backend exception to one closed outcome and reason. Deployment faults
(credentials, sender identity, missing configuration) are retriable: they say
nothing about the recipient. Only a rejection of this recipient or this message
is terminal. A temporary 4xx SMTP recipient refusal is retriable.
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterable
from enum import StrEnum


class EmailOutcome(StrEnum):
    """Closed classification of one send attempt.

    ``ACCEPTED`` means the backend took the message; it is not mailbox delivery
    or a read. ``RETRIABLE`` is a transient or deployment fault. ``TERMINAL`` is a
    rejection of this recipient or message. ``UNAVAILABLE`` means no delivering
    backend is configured, so nothing was sent.
    """

    ACCEPTED = "accepted"
    RETRIABLE = "retriable"
    TERMINAL = "terminal"
    UNAVAILABLE = "unavailable"


# One classification: the outcome and its closed reason.
Classification = tuple[EmailOutcome, str]


def first_match(rules: Iterable[tuple[bool, Classification]]) -> Classification | None:
    """Return the classification of the first rule whose condition holds."""
    return next((classification for matched, classification in rules if matched), None)


_RETRY_PROVIDER: Classification = (EmailOutcome.RETRIABLE, "provider_unavailable")
_RETRY_DEPLOYMENT: Classification = (EmailOutcome.RETRIABLE, "transport_unavailable")
_RETRY_THROTTLED: Classification = (EmailOutcome.RETRIABLE, "provider_throttled")
_TERMINAL_PROVIDER: Classification = (EmailOutcome.TERMINAL, "provider_rejected")
_TERMINAL_RECIPIENT: Classification = (EmailOutcome.TERMINAL, "recipient_rejected")
_TERMINAL_MESSAGE: Classification = (EmailOutcome.TERMINAL, "invalid_message")


def classify(exc: BaseException) -> Classification:
    """Map a backend exception to a closed outcome and reason.

    Deployment faults (credentials, sender identity, missing configuration) are
    retriable: they say nothing about the recipient. Only a rejection of this
    recipient or this message is terminal.
    """
    classified = (rule(exc) for rule in (_classify_anymail, _classify_ses, _classify_smtp, _classify_generic))
    return next((result for result in classified if result is not None), (EmailOutcome.RETRIABLE, "transport_error"))


def _classify_http_status(status_code: int | None) -> Classification:
    """Classify an ESP HTTP status code."""
    client_error = status_code is not None and 400 <= status_code < 500 and status_code != 408
    rules = [
        (status_code in (401, 403, 404), _RETRY_DEPLOYMENT),
        (status_code == 429, _RETRY_THROTTLED),
        (client_error, _TERMINAL_PROVIDER),
    ]
    return first_match(rules) or _RETRY_PROVIDER


def _classify_anymail(exc: BaseException) -> Classification | None:
    """Classify django-anymail exceptions (GCP SendGrid/Mailgun path)."""
    from anymail.exceptions import (
        AnymailAPIError,
        AnymailConfigurationError,
        AnymailError,
        AnymailInvalidAddress,
        AnymailRecipientsRefused,
    )

    api_error = isinstance(exc, AnymailAPIError)
    return first_match(
        [
            (isinstance(exc, AnymailRecipientsRefused), _TERMINAL_RECIPIENT),
            (isinstance(exc, AnymailInvalidAddress), _TERMINAL_MESSAGE),
            (api_error, _classify_http_status(getattr(exc, "status_code", None)) if api_error else _RETRY_PROVIDER),
            (isinstance(exc, AnymailConfigurationError), _RETRY_DEPLOYMENT),
            (isinstance(exc, AnymailError), _TERMINAL_MESSAGE),
        ]
    )


def _classify_ses(exc: BaseException) -> Classification | None:
    """Classify botocore exceptions (AWS django-ses path)."""
    from botocore.exceptions import BotoCoreError, ClientError

    code = exc.response.get("Error", {}).get("Code", "") if isinstance(exc, ClientError) else ""
    return first_match(
        [
            (code == "MessageRejected", _TERMINAL_PROVIDER),
            (code in ("Throttling", "ThrottlingException", "TooManyRequestsException"), _RETRY_THROTTLED),
            (isinstance(exc, ClientError | BotoCoreError), _RETRY_PROVIDER),
        ]
    )


def _classify_smtp(exc: BaseException) -> Classification | None:
    """Classify smtplib exceptions (SMTP backend path).

    A 4xx RCPT reply (greylisting, mailbox busy) is temporary; a refusal is
    permanent only when every refused recipient got a 5xx reply.
    """
    refused = isinstance(exc, smtplib.SMTPRecipientsRefused)
    codes = [reply[0] for reply in exc.recipients.values()] if isinstance(exc, smtplib.SMTPRecipientsRefused) else []
    permanent_refusal = bool(codes) and all(isinstance(code, int) and code >= 500 for code in codes)
    permanent_reply = isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code >= 500
    return first_match(
        [
            (refused and permanent_refusal, _TERMINAL_RECIPIENT),
            (refused, (EmailOutcome.RETRIABLE, "recipient_deferred")),
            (isinstance(exc, smtplib.SMTPAuthenticationError | smtplib.SMTPSenderRefused), _RETRY_DEPLOYMENT),
            (isinstance(exc, smtplib.SMTPConnectError), _RETRY_PROVIDER),
            (permanent_reply, _TERMINAL_PROVIDER),
            (isinstance(exc, smtplib.SMTPException), _RETRY_PROVIDER),
        ]
    )


def _classify_generic(exc: BaseException) -> Classification | None:
    """Classify network, configuration, and message-construction errors."""
    from django.core.exceptions import ImproperlyConfigured

    return first_match(
        [
            (isinstance(exc, OSError), _RETRY_PROVIDER),
            (isinstance(exc, ImproperlyConfigured), _RETRY_DEPLOYMENT),
            # Django header/address validation (BadHeaderError is ValueError).
            (isinstance(exc, ValueError), _TERMINAL_MESSAGE),
        ]
    )
