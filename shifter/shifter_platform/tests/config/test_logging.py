"""Tests for the platform ECS log formatter (``config.logging``)."""

from __future__ import annotations

import json
import logging

import pytest

from config.logging import ECSFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("cms.services", logging.INFO, __file__, 7, "launch accepted range_id=%s", (42,), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_emits_only_allowlisted_labels(settings) -> None:
    settings.ENVIRONMENT = "dev"

    payload = json.loads(ECSFormatter(environment="fallback").format(_record(range_id=42, gap_codes=["x"])))

    assert payload["message"] == "launch accepted range_id=42"
    assert payload["service.environment"] == "dev"
    assert payload["labels.range_id"] == 42
    # Keys outside LABEL_FIELDS never reach the rendered record, so values
    # that operators need belong in the message.
    assert not any("gap_codes" in key for key in payload)


def test_formatter_keeps_constructor_environment_without_django_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.conf import settings

    monkeypatch.setattr(type(settings), "configured", property(lambda _self: False))

    payload = json.loads(ECSFormatter(environment="standalone").format(_record()))

    assert payload["service.environment"] == "standalone"
