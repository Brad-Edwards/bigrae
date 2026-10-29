"""Runtime refuses partial cutover state or a different policy binding."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from config.openfga_authorization import OpenFgaRuntimeSettings
from workspaces.models import IdentityCutoverState

pytestmark = pytest.mark.django_db

STORE_ID = "01J00000000000000000000001"
MODEL_ID = "01J00000000000000000000000"


def _settings(*, store_id: str = STORE_ID) -> OpenFgaRuntimeSettings:
    return OpenFgaRuntimeSettings(
        api_url="https://openfga.internal.example",
        store_id=store_id,
        model_id=MODEL_ID,
        api_token="test-token",
        ca_cert_path="/run/secrets/openfga-ca/ca.crt",
    )


def _activated() -> None:
    now = timezone.now()
    IdentityCutoverState.objects.create(
        state=IdentityCutoverState.State.ACTIVATED,
        plan_digest="a" * 64,
        tuple_digest="b" * 64,
        provider_store_id=STORE_ID,
        model_id=MODEL_ID,
        bootstrap_principal_uuid="00000000-0000-0000-0000-000000000001",
        bootstrap_verified_at=now,
        admission_invalidated_at=now,
    )


def test_runtime_check_refuses_missing_cutover() -> None:
    with pytest.raises(CommandError, match="not activated"):
        call_command("identity_runtime_check")


def test_runtime_check_accepts_exact_activated_binding(monkeypatch) -> None:
    _activated()
    monkeypatch.setattr("config.identity_runtime.runtime_settings_from_django", _settings)
    output = StringIO()
    call_command("identity_runtime_check", stdout=output)
    assert output.getvalue().strip() == '{"identity_runtime":"ready"}'


def test_runtime_check_refuses_different_store(monkeypatch) -> None:
    _activated()
    monkeypatch.setattr(
        "config.identity_runtime.runtime_settings_from_django",
        lambda: _settings(store_id="01J00000000000000000000002"),
    )
    with pytest.raises(CommandError, match="does not match"):
        call_command("identity_runtime_check")
