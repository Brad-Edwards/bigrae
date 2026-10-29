"""RAES dispatch admission, receipts and secret-free failure diagnostics."""

from __future__ import annotations

import pytest
from raes_contracts.runtime_state import RuntimeSnapshot

from shared.raes.runtime_target import RAES_PROVISIONING_PLAN_KIND, ShifterProvisioner
from tests.shared.raes.test_runtime_target import FakeDispatchPort, _network, _node, _plan

# --- apply / dispatch ----------------------------------------------------------


def test_enqueue_dispatches_serialized_plan_and_reports_a_receipt() -> None:
    port = FakeDispatchPort()
    plan = _plan(_node("provision.node.web", "web", links=("lan",)), _network("provision.network.lan", "lan"))
    result = ShifterProvisioner(port).enqueue(plan)
    assert result.accepted is True
    assert set(result.addresses) == {"provision.node.web", "provision.network.lan"}
    assert len(port.plans) == 1
    assert port.plans[0]["kind"] == RAES_PROVISIONING_PLAN_KIND


def test_apply_does_not_dispatch_on_invalid_plan() -> None:
    port = FakeDispatchPort()
    result = ShifterProvisioner(port).apply(_plan(_node("provision.node.a", "a", os_family="macos")), RuntimeSnapshot())
    assert result.success is False
    assert port.plans == []


def test_apply_wraps_dispatch_failure_as_diagnostic() -> None:
    port = FakeDispatchPort(raises=RuntimeError("boom"))
    plan = _plan(_node("provision.node.web", "web"))
    result = ShifterProvisioner(port).apply(plan, RuntimeSnapshot())
    assert result.success is False
    assert any(d.code == "shifter-provisioner.dispatch-failed" for d in result.diagnostics)


@pytest.mark.parametrize("method", ["apply", "enqueue"])
def test_dispatch_diagnostics_do_not_expose_provider_secrets(method, caplog) -> None:
    secret = "password=SYNTHETIC_DISPATCH_SECRET https://example.invalid/?token=SYNTHETIC_TOKEN"
    provisioner = ShifterProvisioner(FakeDispatchPort(raises=RuntimeError(secret)))
    plan = _plan(_node("provision.node.web", "web"))
    result = provisioner.apply(plan, RuntimeSnapshot()) if method == "apply" else provisioner.enqueue(plan)
    [failure] = [d for d in result.diagnostics if d.code == "shifter-provisioner.dispatch-failed"]
    assert failure.message == "Provisioning dispatch failed (RuntimeError)"
    assert "SYNTHETIC_" not in caplog.text
    assert "SYNTHETIC_" not in str(result)


def test_apply_and_validate_reject_non_plan() -> None:
    provisioner = ShifterProvisioner(FakeDispatchPort())
    assert provisioner.validate("nope")[0].code == "shifter-provisioner.invalid-plan"
    result = provisioner.apply("nope", RuntimeSnapshot())
    assert result.success is False


def test_validate_clean_plan_has_no_errors() -> None:
    plan = _plan(_node("provision.node.web", "web", links=("lan",)), _network("provision.network.lan", "lan"))
    assert [d for d in ShifterProvisioner(FakeDispatchPort()).validate(plan) if d.is_error] == []
