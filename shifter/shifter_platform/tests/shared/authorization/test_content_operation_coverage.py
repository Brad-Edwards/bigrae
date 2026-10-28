"""Published control-plane operations require explicit owning authority locators."""

import json
from pathlib import Path

from shared.authorization import action_definition


def test_all_published_cms_and_mission_control_operations_have_exact_coverage():
    from cms.api.operation_policy import API_OPERATION_POLICY as cms
    from mission_control.api.operation_policy import API_OPERATION_POLICY as mission_control

    schema = json.loads((Path(__file__).parents[3] / "openapi" / "v1.json").read_text())
    published = {
        operation["operationId"]
        for path, methods in schema["paths"].items()
        if path.startswith(("/api/v1/cms/", "/api/v1/mission-control/"))
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    policies = cms | mission_control
    assert not (set(cms) & set(mission_control))
    assert set(policies) == published, "Classify every added or removed operation through its owning service"
    for policy in policies.values():
        assert policy.actions and policy.locator and policy.target
        for action in policy.actions:
            action_definition(action)


def test_worker_callback_and_stream_effects_have_explicit_authority_sources():
    from cms.api.operation_policy import EXTERNAL_EFFECT_POLICY
    from engine.services.operation_policy import DURABLE_EFFECT_POLICY
    from mission_control.api.operation_policy import CHANNEL_POLICY

    assert set(EXTERNAL_EFFECT_POLICY) == {
        "preparation_worker_input",
        "preparation_worker_result",
        "vpn_connect",
        "vpn_heartbeat",
        "vpn_end",
    }
    assert {
        "provision",
        "retry",
        "cancel",
        "cleanup",
        "result",
        "plugin",
        "preparation",
    } <= DURABLE_EFFECT_POLICY.keys()
    assert {"ssh", "range_status", "ngfw_status", "guacamole", "vpn"} == CHANNEL_POLICY.keys()
    assert DURABLE_EFFECT_POLICY["cleanup"] == "explicit_system_resource_generation"
    assert DURABLE_EFFECT_POLICY["result"] == "authenticated_workload_and_exact_attempt"
    assert all("principal" in value or "workload" in value for value in EXTERNAL_EFFECT_POLICY.values())
