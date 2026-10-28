"""The manage.py entry point sends uncaught command exceptions through LOGGING."""

from __future__ import annotations

import logging
import sys

import pytest

import manage


def test_uncaught_command_exception_is_logged_with_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(RuntimeError) as raised:
        raise RuntimeError("drain cycle failed")
    exc_type, exc, traceback = raised.type, raised.value, raised.tb

    with caplog.at_level(logging.CRITICAL, logger="manage"):
        manage.log_uncaught_exception(exc_type, exc, traceback)

    [record] = [r for r in caplog.records if r.name == "manage"]
    assert record.levelno == logging.CRITICAL
    assert record.exc_info is not None
    assert record.exc_info[1] is exc


def test_keyboard_interrupt_keeps_the_default_hook(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    delegated: list[type[BaseException]] = []
    monkeypatch.setattr(sys, "__excepthook__", lambda exc_type, _exc, _tb: delegated.append(exc_type))
    with pytest.raises(KeyboardInterrupt) as raised:
        raise KeyboardInterrupt
    exc_type, exc, traceback = raised.type, raised.value, raised.tb

    manage.log_uncaught_exception(exc_type, exc, traceback)

    assert delegated == [KeyboardInterrupt]
    assert not [r for r in caplog.records if r.name == "manage"]


def test_main_installs_the_hook_before_running_the_command(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["manage.py", "version"])
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)

    manage.main()

    assert sys.excepthook is manage.log_uncaught_exception
    assert capsys.readouterr().out.strip()
