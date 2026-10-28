"""Both public names expose the same contract classes and isolation boundary."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module", ["guest", "model_access", "preparation", "runtime", "verification", "verification.contracts"]
)
def test_contract_types_are_not_duplicated(module):
    old = importlib.import_module("shifter_adapter_sdk." + module)
    new = importlib.import_module("bigrae_adapter_sdk." + module)
    for name, value in vars(old).items():
        if not name.startswith("_") and isinstance(value, type) and value.__module__.startswith("shifter_adapter_sdk"):
            assert getattr(new, name) is value
