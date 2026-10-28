"""The CTF scheduler copies organizer images after its due tasks (#2090)."""

from __future__ import annotations

import pytest

from ctf.management.commands import run_ctf_scheduler as scheduler


def test_cycle_copies_images_after_due_tasks(monkeypatch):
    calls: list[str] = []
    command = scheduler.Command()
    monkeypatch.setattr(command, "_recover_stale_tasks", lambda: calls.append("recover"))
    monkeypatch.setattr(scheduler, "_run_retention_maintenance", lambda: calls.append("retention"))
    monkeypatch.setattr(command, "_process_due_tasks", lambda batch_size: calls.append(f"tasks:{batch_size}"))
    received: dict = {}

    def ingest(**kwargs):
        received.update(kwargs)
        calls.append("images")
        return 0

    monkeypatch.setattr("ctf.services.content_images.ingest_pending_content_images", ingest)

    command._run_cycle(7)

    assert calls == ["recover", "retention", "tasks:7", "images"]
    # Copies keep the liveness heartbeat fresh while they run (core-F3, #2090).
    assert received["heartbeat"] == command._touch_heartbeat


def test_image_copy_failure_does_not_stop_the_cycle(monkeypatch):
    command = scheduler.Command()
    logged: list[str] = []
    monkeypatch.setattr(scheduler.logger, "exception", lambda message, *_a, **_k: logged.append(message))
    processed: list[int] = []
    monkeypatch.setattr(command, "_recover_stale_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "_run_retention_maintenance", lambda: None)
    monkeypatch.setattr(command, "_process_due_tasks", lambda batch_size: processed.append(batch_size))

    def broken(**_kw):
        raise RuntimeError("copy crashed")

    monkeypatch.setattr("ctf.services.content_images.ingest_pending_content_images", broken)

    command._run_cycle(3)

    assert processed == [3]
    assert logged == ["CTF content image copy sweep failed"]


def test_cycle_skips_image_copy_during_shutdown(monkeypatch):
    command = scheduler.Command()
    command.shutdown = True
    monkeypatch.setattr(command, "_recover_stale_tasks", lambda: None)
    monkeypatch.setattr(scheduler, "_run_retention_maintenance", lambda: None)
    monkeypatch.setattr(command, "_process_due_tasks", lambda batch_size: None)
    monkeypatch.setattr(
        "ctf.services.content_images.ingest_pending_content_images",
        lambda **_kw: pytest.fail("image copy started during shutdown"),
    )

    command._run_cycle(3)
