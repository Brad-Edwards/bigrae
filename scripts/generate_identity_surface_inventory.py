#!/usr/bin/env python3
"""Generate the reviewed S8 HTTP/non-HTTP authorization surface snapshot."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "shifter/shifter_platform/openapi/v1.json"
OUTPUT = ROOT / "shifter/shifter_platform/tests/contracts/identity_authorization_surfaces.json"
BASELINE_COMMIT = "8aee758331286293cdc837d94eb95fafa5674ae0"
METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
PLATFORM = ROOT / "shifter/shifter_platform"

sys.path.insert(0, str(ROOT / "shifter/shifter_platform"))

from cms.api.operation_policy import API_OPERATION_POLICY as CMS_OPERATION_POLICY  # noqa: E402
from config.model_access_operation_policy import API_OPERATION_POLICY as MODEL_ACCESS_OPERATION_POLICY  # noqa: E402
from ctf.api.operation_policy import API_OPERATION_POLICY as CTF_OPERATION_POLICY  # noqa: E402
from mission_control.api.operation_policy import API_OPERATION_POLICY as MC_OPERATION_POLICY  # noqa: E402

EXACT_OPERATION_POLICY = {
    **CMS_OPERATION_POLICY,
    **CTF_OPERATION_POLICY,
    **MC_OPERATION_POLICY,
    **MODEL_ACCESS_OPERATION_POLICY,
}


def _operations(document: dict) -> dict[tuple[str, str], dict]:
    return {
        (path, method): operation
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method in METHODS
    }


def _policy_mapping(path: str, operation: dict) -> dict[str, str]:
    """Record exact prepared action/target/locator bindings when one exists."""
    if operation.get("security") == []:
        return {"decision": "public-contract", "target": "none", "scope": "none"}
    policy = EXACT_OPERATION_POLICY.get(operation["operationId"])
    if policy is not None:
        suffix = f";selector={policy.selector}" if policy.selector else ""
        if policy.exemption:
            suffix += f";exemption={policy.exemption}"
        return {
            "decision": "|".join(policy.actions) if policy.actions else f"exempt:{policy.exemption}",
            "target": policy.target or "none",
            "scope": f"sql:{policy.locator or 'none'}{suffix}",
        }
    if path.startswith(
        (
            "/api/v1/accounts/",
            "/api/v1/account-workspaces/",
            "/api/v1/workspaces/",
            "/api/v1/administer/",
            "/api/v1/audit/",
        )
    ):
        return {"decision": "registered-action", "target": "route-resource", "scope": "sql-ancestry"}
    if path.startswith("/api/v1/ctf/"):
        return {"decision": "ctf-operation-policy", "target": "event-or-child", "scope": "event-account"}
    if path.startswith(("/api/v1/cms/", "/api/v1/mission-control/")):
        return {"decision": "owning-service-action", "target": "requested-resource", "scope": "resource-account"}
    return {"decision": "authenticated-platform-contract", "target": "operation-resource", "scope": "explicit"}


def _row(kind: str, surface: str, decision: str, target: str, scope: str, disposition: str = "retained") -> list[str]:
    return [kind, surface, disposition, decision, target, scope]


def _management_commands() -> list[list[str]]:
    """Discover every repository-owned Django command as an exact operator surface."""
    rows = []
    for path in sorted(PLATFORM.glob("*/management/commands/*.py")):
        if path.name == "__init__.py":
            continue
        app = path.relative_to(PLATFORM).parts[0]
        rows.append(
            _row(
                "management-command",
                f"{app}:{path.stem}",
                "deployment-command",
                "installation",
                "deployment-operator",
            )
        )
    return rows


def _registration_surfaces(path: Path) -> list[str]:
    """Read model expressions returned by one domain registration facade."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    surfaces = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "policy_admin_registrations":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or not isinstance(child.value, (ast.Tuple, ast.List)):
                continue
            for registration in child.value.elts:
                if isinstance(registration, (ast.Tuple, ast.List)) and registration.elts:
                    surfaces.append(ast.unparse(registration.elts[0]))
    return surfaces


def _policy_admin_models() -> list[list[str]]:
    """Discover direct and owner-domain policy-admin registrations."""
    tree = ast.parse((PLATFORM / "config/policy_admin_site.py").read_text(encoding="utf-8"))
    surfaces = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if ast.unparse(node.func.value) != "policy_admin_site" or node.func.attr != "register" or not node.args:
            continue
        surface = ast.unparse(node.args[0])
        if surface != "model":
            surfaces.append(surface)
    surfaces.extend(_registration_surfaces(PLATFORM / "management/services.py"))
    surfaces.extend(_registration_surfaces(PLATFORM / "workspaces/services/__init__.py"))
    rows = [
        _row("django-admin", surface, "registered-action-readonly", "model-resource", "policy-admin-site")
        for surface in surfaces
    ]
    return sorted(rows, key=lambda row: row[1])


def _websocket_routes() -> list[list[str]]:
    """Discover each mounted repository WebSocket route from the owning routers."""
    rows = []
    for relative in ("mission_control/routing.py", "shared/routing.py"):
        path = PLATFORM / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or ast.unparse(node.func) != "re_path" or not node.args:
                continue
            route = ast.unparse(node.args[0])
            notification = relative == "shared/routing.py" and ast.literal_eval(node.args[0]) == "ws/notifications/$"
            rows.append(
                _row(
                    "socket",
                    f"{relative}:{route}",
                    "event.read|event.participate" if notification else "continuing-access-check",
                    "event" if notification else "connection",
                    "credential-bound-topic-and-live-participation" if notification else "resource-account",
                )
            )
    return sorted(rows, key=lambda row: row[1])


def main() -> int:
    current = json.loads(OPENAPI.read_text(encoding="utf-8"))
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to generate the identity surface inventory")
    baseline_raw = subprocess.check_output(  # noqa: S603
        [git, "show", f"{BASELINE_COMMIT}:shifter/shifter_platform/openapi/v1.json"],
        cwd=ROOT,
        text=True,
    )
    baseline = json.loads(baseline_raw)
    current_ops = _operations(current)
    baseline_ops = _operations(baseline)
    http = []
    for key, operation in sorted(current_ops.items()):
        path, method = key
        http.append(
            {
                "path": path,
                "method": method.upper(),
                "operation_id": operation["operationId"],
                "disposition": "retained" if key in baseline_ops else "added",
                "authority": _policy_mapping(path, operation),
            }
        )
    for key, operation in sorted(baseline_ops.items()):
        if key not in current_ops:
            http.append(
                {
                    "path": key[0],
                    "method": key[1].upper(),
                    "operation_id": operation["operationId"],
                    "disposition": "retired",
                    "authority": {"decision": "not-mounted", "target": "none", "scope": "none"},
                }
            )
    non_http = [
        *_policy_admin_models(),
        _row(
            "django-admin",
            "unregistered-model-writes",
            "canonical-service-only",
            "model-resource",
            "explicit",
            "retired",
        ),
        *_management_commands(),
        ["queue", "provisioner-launch", "retained", "dispatch-reauthorization", "range-or-device", "recorded-scope"],
        ["queue", "operation-result", "retained", "recorded-authority", "operation", "recorded-scope"],
        ["scheduler", "ctf", "retained", "service-principal-action", "event", "event-account"],
        ["worker", "ctf-communication", "retained", "service-principal-action", "event", "event-account"],
        ["worker", "range-lifecycle", "retained", "dispatch-reauthorization", "range", "resource-account"],
        ["callback", "guacamole-authority", "retained", "continuing-access-check", "connection", "resource-account"],
        ["callback", "openvpn-authority", "retained", "continuing-access-check", "vpn-session", "resource-account"],
        *_websocket_routes(),
        ["webhook", "identity-provider", "retained", "provider-binding-proof", "principal", "installation"],
        ["cronjob", "maintenance", "retained", "service-principal-action", "owned-records", "bounded-system"],
        ["legacy", "personal-workspace-authority", "retired", "not-mounted", "none", "none"],
        ["legacy", "staff-superuser-authority", "retired", "not-authoritative", "none", "none"],
        [
            "legacy",
            "shared-vpn-secret-delivery",
            "retired",
            "per-credential-certificate",
            "vpn-profile",
            "resource-account",
        ],
    ]
    payload = {
        "schema": "shifter.identity-authorization-surfaces/v1",
        "historical_baseline": {"commit": BASELINE_COMMIT, "operation_count": len(baseline_ops)},
        "current_operation_count": len(current_ops),
        "http": sorted(http, key=lambda item: (item["path"], item["method"], item["operation_id"])),
        "non_http": [
            {
                "kind": row[0],
                "surface": row[1],
                "disposition": row[2],
                "decision": row[3],
                "target": row[4],
                "scope": row[5],
            }
            for row in non_http
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
