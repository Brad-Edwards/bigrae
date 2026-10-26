"""Canonical and compatibility names meet before setting validation."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from config._product_env import product_env


def test_canonical_name(monkeypatch):
    monkeypatch.setenv("BIGRAE_SUPPORT_EMAIL", "support@example.test")
    monkeypatch.delenv("SHIFTER_SUPPORT_EMAIL", raising=False)
    assert product_env("SHIFTER_SUPPORT_EMAIL") == "support@example.test"


def test_legacy_name_and_default(monkeypatch, caplog):
    monkeypatch.delenv("BIGRAE_SUPPORT_EMAIL", raising=False)
    monkeypatch.setenv("SHIFTER_SUPPORT_EMAIL", "support@example.test")
    assert product_env("SHIFTER_SUPPORT_EMAIL") == "support@example.test"
    assert "support@example.test" not in caplog.text
    monkeypatch.delenv("SHIFTER_SUPPORT_EMAIL")
    assert product_env("SHIFTER_SUPPORT_EMAIL", "fallback") == "fallback"


def test_equal_aliases_are_one_value(monkeypatch):
    monkeypatch.setenv("BIGRAE_SUPPORT_EMAIL", "same")
    monkeypatch.setenv("SHIFTER_SUPPORT_EMAIL", "same")
    assert product_env("SHIFTER_SUPPORT_EMAIL") == "same"


def test_conflict_fails_without_disclosing_either_value(monkeypatch):
    monkeypatch.setenv("BIGRAE_SUPPORT_EMAIL", "private-one")
    monkeypatch.setenv("SHIFTER_SUPPORT_EMAIL", "private-two")
    with pytest.raises(ImproperlyConfigured, match="conflicting") as error:
        product_env("SHIFTER_SUPPORT_EMAIL")
    assert "private-one" not in str(error.value)
    assert "private-two" not in str(error.value)
