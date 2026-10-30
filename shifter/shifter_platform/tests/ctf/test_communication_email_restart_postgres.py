"""Real worker-process crash, restart, and graceful-stop proofs for email (#1525).

Each test runs ``manage.py drain_ctf_communication_deliveries`` as a separate OS
process against the PostgreSQL test database, then kills or signals it. Only the
external mail transport is replaced (``tests.ctf.email_process_backends``,
ADR-019-R1); the worker, adapter, ledger, and ``shared.email`` run for real.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection
from django.utils import timezone

from ctf.enums import ParticipantStatus
from ctf.enums_communication import DeliveryStatus, IntentDeliveryState
from ctf.models import CommunicationIntent, CTFParticipant, DeliveryAttempt
from ctf.services.communication import CampaignDraft, create_campaign, release_campaign
from tests.ctf._account_scope import individual_account_uuid

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

PLATFORM_DIR = Path(__file__).resolve().parents[2]
BACKENDS = "tests.ctf.email_process_backends"


def _release(owner, event, count):
    for index in range(count):
        CTFParticipant.objects.create(
            event=event,
            email=f"restart{index}@test.com",
            name=f"restart{index}",
            status=ParticipantStatus.ACTIVE.value,
            registered_at=timezone.now(),
        )
    draft = CampaignDraft(
        title="Restart",
        origin="organizer_staff",
        target_event_ids=[event.id],
        audience_spec={"kind": "event", "event_ids": [str(event.id)]},
        trigger_spec={"kind": "manual"},
        channels=["email"],
        subject="Check-in",
        body="Report to the event desk.",
    )
    campaign = create_campaign(owner, individual_account_uuid(owner), draft, parent_type="account")
    return release_campaign(campaign, occurrence_key="restart", actor_user_id=owner.id)


def _environment(directory: Path, backend: str) -> dict[str, str]:
    """Point a worker process at this test database and a stand-in transport."""
    database = connection.settings_dict
    env = dict(os.environ)
    env.update(
        {
            "TESTING": "1",
            "DJANGO_DEBUG": "true",
            "TEST_DB_BACKEND": "postgres",
            "DB_NAME": str(database["NAME"]),
            "DB_HOST": str(database["HOST"]),
            "DB_PORT": str(database["PORT"]),
            "DB_USER": str(database["USER"]),
            "DB_PASSWORD": str(database["PASSWORD"] or ""),
            "FIELD_ENCRYPTION_KEY": settings.FIELD_ENCRYPTION_KEY,
            "EMAIL_BACKEND": f"{BACKENDS}.{backend}",
            "SHIFTER_TEST_EMAIL_DIR": str(directory),
            # The adapter builds the portal link from a safe public origin (#580).
            "SITE_URL": "https://ctf.example.test",
            # Metrics stay fail-soft and offline: a closed local endpoint, no retries.
            "CLOUD_PROVIDER": "aws",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-2",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
            "AWS_MAX_ATTEMPTS": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return env


def _worker(env: dict[str, str], *args: str) -> subprocess.Popen[str]:
    """Start the real drain command as its own process (fixed argv, no shell)."""
    command = [sys.executable, "manage.py", "drain_ctf_communication_deliveries", *args]
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and command, no untrusted input
        command, cwd=PLATFORM_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(f"worker exited early: {process.communicate()[1][-2000:]}")
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path.name}")


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_killed_worker_is_recovered_by_a_restarted_process(organizer_user, ctf_event, tmp_path):
    intent = _release(organizer_user, ctf_event, 1)
    first = _worker(_environment(tmp_path, "HangingBackend"))
    try:
        _wait_for(tmp_path / "started.jsonl", first)
    finally:
        first.kill()
        first.communicate(timeout=30)

    attempt = DeliveryAttempt.objects.get()
    # The crash left an indeterminate claim: the attempt boundary was stamped, no outcome.
    assert attempt.status == DeliveryStatus.CLAIMED.value
    assert attempt.attempted_at is not None
    assert attempt.attempt_number == 1
    # Simulate the lease running out instead of waiting its full length.
    DeliveryAttempt.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))

    second = _worker(_environment(tmp_path, "RecordingBackend"))
    _out, err = second.communicate(timeout=120)
    assert second.returncode == 0, err[-2000:]

    attempt.refresh_from_db()
    assert attempt.status == DeliveryStatus.ACCEPTED.value
    assert attempt.attempt_number == 2
    assert CommunicationIntent.objects.get(pk=intent.pk).delivery_state == IntentDeliveryState.ACCEPTED.value
    started, accepted = _lines(tmp_path / "started.jsonl"), _lines(tmp_path / "accepted.jsonl")
    assert [line["to"] for line in accepted] == [["restart0@test.com"]]
    # The recovered send reuses the command's Message-ID (at-least-once, same identity).
    assert accepted[0]["message_id"] == started[0]["message_id"] != ""


def test_sigterm_finishes_the_attempt_in_flight_and_releases_the_rest(organizer_user, ctf_event, tmp_path):
    _release(organizer_user, ctf_event, 3)
    worker = _worker(_environment(tmp_path, "HangingBackend"), "--loop", "--interval", "1")
    try:
        _wait_for(tmp_path / "started.jsonl", worker)
        worker.send_signal(signal.SIGTERM)
        (tmp_path / "release").touch()
        out, err = worker.communicate(timeout=60)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate(timeout=30)

    assert worker.returncode == 0, err[-2000:]
    assert "released=2" in out
    assert len(_lines(tmp_path / "accepted.jsonl")) == 1
    assert DeliveryAttempt.objects.filter(status=DeliveryStatus.ACCEPTED.value).count() == 1
    released = DeliveryAttempt.objects.exclude(status=DeliveryStatus.ACCEPTED.value)
    assert released.count() == 2
    assert all(
        (row.status, row.attempt_number, row.lease_token) == (DeliveryStatus.QUEUED.value, 0, "") for row in released
    )
