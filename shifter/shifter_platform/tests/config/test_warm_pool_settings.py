"""Fail-closed composition tests for the warm-pool worker credential."""

from __future__ import annotations

import importlib
import os

import pytest
from django.core.exceptions import ImproperlyConfigured

MODULE = "config._warm_pool_settings"
POLICY_KEY = "WARM_POOL_POLICY_JSON"
CREDENTIAL_KEY = "WARM_POOL_SERVICE_CREDENTIAL_UUID"
ACTIVE_POLICY = (
    '{"enabled":true,"max_total_ready":1,"buckets":[{"id":"gce-example",'
    '"backend":"gce","scenario":"example","capacity_partition":"default",'
    '"target":1,"minimum":0,"maximum":1,"idle_ttl_seconds":3600}]}'
)
CREDENTIAL_UUID = "11111111-1111-4111-8111-111111111111"


def _reload(monkeypatch, *, policy: str = "", credential: str = ""):
    monkeypatch.setenv(POLICY_KEY, policy)
    monkeypatch.setenv(CREDENTIAL_KEY, credential)
    return importlib.reload(importlib.import_module(MODULE))


@pytest.fixture(autouse=True)
def _restore_module():
    yield
    os.environ.pop(POLICY_KEY, None)
    os.environ.pop(CREDENTIAL_KEY, None)
    importlib.reload(importlib.import_module(MODULE))


def test_disabled_policy_does_not_require_worker_credential(monkeypatch) -> None:
    module = _reload(monkeypatch)
    assert not module.WARM_POOL_POLICY.is_active()
    assert module.WARM_POOL_SERVICE_CREDENTIAL_UUID is None


def test_active_policy_requires_worker_credential(monkeypatch) -> None:
    with pytest.raises(ImproperlyConfigured, match=CREDENTIAL_KEY):
        _reload(monkeypatch, policy=ACTIVE_POLICY)


@pytest.mark.parametrize(
    "credential",
    [
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
def test_worker_credential_must_be_canonical_non_nil_uuid(monkeypatch, credential: str) -> None:
    with pytest.raises(ImproperlyConfigured, match=CREDENTIAL_KEY):
        _reload(monkeypatch, credential=credential)


def test_active_policy_binds_exact_worker_credential(monkeypatch) -> None:
    module = _reload(monkeypatch, policy=ACTIVE_POLICY, credential=CREDENTIAL_UUID)
    assert module.WARM_POOL_POLICY.is_active()
    assert str(module.WARM_POOL_SERVICE_CREDENTIAL_UUID) == CREDENTIAL_UUID
