"""S8 surface inventory is complete and matches the production contract."""

import json
from pathlib import Path

from cms.api.operation_policy import API_OPERATION_POLICY as CMS_OPERATION_POLICY
from config.model_access_operation_policy import API_OPERATION_POLICY as MODEL_ACCESS_OPERATION_POLICY
from ctf.api.operation_policy import API_OPERATION_POLICY as CTF_OPERATION_POLICY
from mission_control.api.operation_policy import API_OPERATION_POLICY as MC_OPERATION_POLICY
from shared.api.contract import generate_openapi_document
from shared.authorization import ACTION_CATALOG

INVENTORY = Path(__file__).with_name("identity_authorization_surfaces.json")
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def test_historical_and_current_http_operations_have_one_disposition_and_mapping() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    current = json.loads(generate_openapi_document())
    current_keys = {
        (path, method.upper(), operation["operationId"])
        for path, path_item in current["paths"].items()
        for method, operation in path_item.items()
        if method in METHODS
    }
    rows = inventory["http"]
    live_rows = {
        (row["path"], row["method"], row["operation_id"]) for row in rows if row["disposition"] in {"retained", "added"}
    }
    assert inventory["historical_baseline"]["operation_count"] == 220
    assert inventory["current_operation_count"] == len(current_keys)
    assert live_rows == current_keys
    assert len(rows) == len({(row["path"], row["method"], row["operation_id"]) for row in rows})
    assert all(row["disposition"] in {"retained", "added", "retired"} for row in rows)
    assert all(set(row["authority"]) == {"decision", "target", "scope"} for row in rows)


def test_exact_operation_policy_values_match_the_reviewed_inventory() -> None:
    rows = {
        row["operation_id"]: row["authority"]
        for row in json.loads(INVENTORY.read_text(encoding="utf-8"))["http"]
        if row["disposition"] in {"retained", "added"}
    }
    policies = {**CMS_OPERATION_POLICY, **CTF_OPERATION_POLICY, **MC_OPERATION_POLICY, **MODEL_ACCESS_OPERATION_POLICY}
    assert MODEL_ACCESS_OPERATION_POLICY.keys() <= rows.keys()
    registered_actions = {action.code for action in ACTION_CATALOG}
    for operation_id, policy in policies.items():
        assert set(policy.actions) <= registered_actions, operation_id
        assert policy.actions or policy.exemption, operation_id
        if operation_id not in rows:
            continue
        suffix = f";selector={policy.selector}" if policy.selector else ""
        if policy.exemption:
            suffix += f";exemption={policy.exemption}"
        assert rows[operation_id] == {
            "decision": "|".join(policy.actions) if policy.actions else f"exempt:{policy.exemption}",
            "target": policy.target or "none",
            "scope": f"sql:{policy.locator or 'none'}{suffix}",
        }
    assert not any(
        row["authority"]["decision"] in {"ctf-operation-policy", "owning-service-action"}
        for row in json.loads(INVENTORY.read_text(encoding="utf-8"))["http"]
        if row["disposition"] in {"retained", "added"}
    )
    assert all(
        row["authority"]["decision"] != "authenticated-platform-contract"
        for row in json.loads(INVENTORY.read_text(encoding="utf-8"))["http"]
        if row["disposition"] != "retired" and row["path"].startswith("/api/v1/model-access/")
    )


def test_non_http_effect_categories_have_explicit_decision_target_scope_and_disposition() -> None:
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))["non_http"]
    assert len(rows) >= 60
    assert len(rows) == len({(row["kind"], row["surface"]) for row in rows})
    assert all(set(row) == {"kind", "surface", "disposition", "decision", "target", "scope"} for row in rows)
    assert all(row["disposition"] in {"retained", "retired"} for row in rows)


def test_every_repository_management_command_is_an_exact_inventory_surface() -> None:
    platform = Path(__file__).resolve().parents[2]
    expected = {
        f"{path.relative_to(platform).parts[0]}:{path.stem}"
        for path in platform.glob("*/management/commands/*.py")
        if path.name != "__init__.py"
    }
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))["non_http"]
    recorded = {row["surface"] for row in rows if row["kind"] == "management-command"}
    assert recorded == expected


def _assert_production_non_http_inventory(rows) -> None:
    import ast

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from config.policy_admin_site import policy_admin_site
    from mission_control import routing as mc_routing
    from shared import routing as shared_routing

    admin = {row["surface"] for row in rows if row["kind"] == "django-admin" and row["disposition"] == "retained"}
    modules = {"mission_control/routing.py": mc_routing, "shared/routing.py": shared_routing}
    sockets = set()
    for row in rows:
        if row["kind"] != "socket":
            continue
        path, expression = row["surface"].split(":", 1)
        module = modules[path]
        node = ast.parse(expression, mode="eval").body
        if isinstance(node, ast.Constant):
            regex = ast.literal_eval(node)
        else:
            assert isinstance(node, ast.JoinedStr)
            parts = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(part.value)
                else:
                    assert isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name)
                    parts.append(getattr(module, part.value.id))
            assert all(isinstance(part, str) for part in parts)
            regex = "".join(parts)
        sockets.add((path, regex))
    recorded_models = set()
    for expression in admin:
        matches = (
            [get_user_model()]
            if expression == "get_user_model()"
            else [model for model in apps.get_models() if model.__name__ == expression]
        )
        assert len(matches) == 1, expression
        recorded_models.add(matches[0])
    assert recorded_models == set(policy_admin_site._registry)
    expected_sockets = {
        (path, str(route.pattern))
        for path, module in (("mission_control/routing.py", mc_routing), ("shared/routing.py", shared_routing))
        for route in module.websocket_urlpatterns
    }
    assert sockets == expected_sockets


def test_policy_admin_and_websocket_inventory_is_resolver_source_derived() -> None:
    _assert_production_non_http_inventory(json.loads(INVENTORY.read_text(encoding="utf-8"))["non_http"])


def test_unclassified_admin_registration_fails_inventory(monkeypatch):
    import pytest

    from config.policy_admin_site import policy_admin_site
    from workspaces.models import Account

    monkeypatch.setattr(policy_admin_site, "_registry", {**policy_admin_site._registry, Account: object()})
    with pytest.raises(AssertionError):
        test_policy_admin_and_websocket_inventory_is_resolver_source_derived()


def test_unclassified_socket_fails_inventory(monkeypatch):
    import pytest
    from django.urls import re_path

    from mission_control import routing

    monkeypatch.setattr(
        routing,
        "websocket_urlpatterns",
        [*routing.websocket_urlpatterns, re_path(r"^ws/synthetic-unclassified/$", lambda: None)],
    )
    with pytest.raises(AssertionError):
        test_policy_admin_and_websocket_inventory_is_resolver_source_derived()
