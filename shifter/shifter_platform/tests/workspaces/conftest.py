"""Workspace-test fixtures that stop at external system boundaries."""

from __future__ import annotations

from queue import Queue
from unittest.mock import patch

import pytest


@pytest.fixture
def recorded_workspace_email():
    """Record delivery at Django's SMTP-message boundary.

    Invitation tests drive the real rendering, transaction.on_commit callback,
    and shared email transport seam. Only the third-party SMTP message object is
    replaced, per ADR-019-R1.
    """
    deliveries: Queue = Queue()

    class RecordingMessage:
        def __init__(self, subject=None, body=None, from_email=None, to=None, **kwargs):
            self.subject = subject
            self.body = body
            self.from_email = from_email
            self.to = to
            self.alternatives: list[tuple[str, str]] = []

        def attach_alternative(self, content, mimetype):
            self.alternatives.append((content, mimetype))

        def send(self):
            deliveries.put(self)
            return 1

    with patch("django.core.mail.EmailMultiAlternatives", RecordingMessage):
        yield deliveries


@pytest.fixture
def refused_workspace_email():
    """Make the external SMTP boundary refuse every recipient (ADR-019-R1)."""
    import smtplib

    attempts: list[object] = []

    class RefusingMessage:
        def __init__(self, subject=None, body=None, from_email=None, to=None, **kwargs):
            self.to = to

        def attach_alternative(self, content, mimetype):
            pass

        def send(self):
            attempts.append(self)
            raise smtplib.SMTPRecipientsRefused({self.to[0]: (550, b"mailbox unavailable")})

    with patch("django.core.mail.EmailMultiAlternatives", RefusingMessage):
        yield attempts
