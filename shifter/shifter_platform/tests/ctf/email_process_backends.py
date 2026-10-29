"""Email transport stand-ins for real worker-process tests (ADR-019-R1).

A separate ``manage.py`` process selects one of these through ``EMAIL_BACKEND``.
They replace only the external mail transport and record what crossed it in
``SHIFTER_TEST_EMAIL_DIR`` so the test process can observe the effect.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.core.mail.backends.base import BaseEmailBackend


def _record(name: str, message: object) -> None:
    directory = Path(os.environ["SHIFTER_TEST_EMAIL_DIR"])
    entry = {"to": message.to, "message_id": message.extra_headers.get("Message-ID", "")}
    with (directory / name).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


class HangingBackend(BaseEmailBackend):
    """Reach the transport, then wait until the test writes a ``release`` file."""

    def send_messages(self, email_messages):
        message = email_messages[0]
        _record("started.jsonl", message)
        release = Path(os.environ["SHIFTER_TEST_EMAIL_DIR"]) / "release"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if release.exists():
                _record("accepted.jsonl", message)
                return 1
            time.sleep(0.05)
        raise TimeoutError("transport was never released")


class RecordingBackend(BaseEmailBackend):
    """Accept every message and record it."""

    def send_messages(self, email_messages):
        for message in email_messages:
            _record("accepted.jsonl", message)
        return len(email_messages)
