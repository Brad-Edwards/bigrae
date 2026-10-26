"""S8 surface inventory is complete and matches the production contract."""

import json
from pathlib import Path

from cms.api.operation_policy import API_OPERATION_POLICY as CMS_OPERATION_POLICY
from ctf.api.operation_policy import API_OPERATION_POLICY as CTF_OPERATION_POLICY
from mission_control.api.operation_policy import API_OPERATION_POLICY as MC_OPERATION_POLICY
from shared.api.contract import generate_openapi_document

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
    assert inventory["current_operation_count"] == len(current_keys) == 304
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
    policies = {**CMS_OPERATION_POLICY, **CTF_OPERATION_POLICY, **MC_OPERATION_POLICY}
    for operation_id, policy in policies.items():
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


def test_policy_admin_and_websocket_inventory_is_resolver_source_derived() -> None:
    rows = json.loads(INVENTORY.read_text(encoding="utf-8"))["non_http"]
    admin = {row["surface"] for row in rows if row["kind"] == "django-admin" and row["disposition"] == "retained"}
    sockets = {row["surface"] for row in rows if row["kind"] == "socket"}
    assert admin == {
        "get_user_model()",
        "UserProfile",
        "ActivityLog",
        "WorkspaceInvitation",
        "WorkspaceQuotaPolicy",
        "ApiToken",
    }
    assert len(sockets) == 4
    assert all(surface.startswith(("mission_control/routing.py:", "shared/routing.py:")) for surface in sockets)
