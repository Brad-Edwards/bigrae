"""Weekly OpenFGA datastore credential refresh and restart (#2313)."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from celpy import CELEvalError, Environment, celtypes, json_to_cel

from shared.cloud import policy_credential_refresh as refresh

ARNS = {
    "runtime": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-runtime-datastore",
    "admin": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-admin-datastore",
    "migrator": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/openfga-migrator-datastore",
}
MARKER = "SyntheticRotatedPassword2313"


class _Secrets:
    def __init__(self) -> None:
        self.values = {
            ARNS["runtime"]: {"username": "openfga_runtime_clone", "password": MARKER},
            ARNS["admin"]: {"username": "openfga_admin", "password": MARKER},
            ARNS["migrator"]: {"username": "openfga_migrator", "password": MARKER},
        }

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        return {"SecretString": json.dumps({"engine": "postgres", **self.values[SecretId]})}


class _ApiError(Exception):
    """The Kubernetes client's request error."""


class _Kubernetes:
    def __init__(self) -> None:
        self.secret_patches: list[tuple[str, dict[str, Any]]] = []
        self.deployment_patches: list[tuple[str, dict[str, Any]]] = []

    def patch_namespaced_secret(self, name: str, namespace: str, body: dict[str, Any]) -> None:
        assert namespace == "shifter-platform"
        self.secret_patches.append((name, body))

    def patch_namespaced_deployment(self, name: str, namespace: str, body: dict[str, Any]) -> None:
        assert namespace == "shifter-platform"
        self.deployment_patches.append((name, body))


@pytest.fixture
def clients():
    return _Secrets(), _Kubernetes()


def test_refresh_patches_exact_secrets_then_restarts_only_policy_servers(clients):
    secrets, kubernetes = clients
    refresh.refresh(secrets, kubernetes, kubernetes, ARNS, now="2026-10-29T12:00:00Z", api_errors=(_ApiError,))
    assert [name for name, _ in kubernetes.secret_patches] == [
        "openfga-runtime-datastore",
        "openfga-admin-datastore",
        "openfga-migrator-datastore",
    ]
    assert kubernetes.secret_patches[0][1] == {"stringData": {"username": "openfga_runtime_clone", "password": MARKER}}
    assert [name for name, _ in kubernetes.deployment_patches] == ["openfga", "openfga-admin"]
    annotation = kubernetes.deployment_patches[0][1]["spec"]["template"]["metadata"]["annotations"]
    assert annotation == {"shifter.dev/policy-credentials-refreshed-at": "2026-10-29T12:00:00Z"}


@pytest.mark.parametrize(
    "value",
    [
        {"username": "", "password": MARKER},
        {"username": "openfga_runtime", "password": ""},
        {"username": "Robert'); DROP", "password": MARKER},
        {"password": MARKER},
    ],
)
def test_malformed_secret_changes_nothing_and_hides_values(clients, value, caplog):
    secrets, kubernetes = clients
    secrets.values[ARNS["admin"]] = value
    with pytest.raises(refresh.RefreshError) as raised:
        refresh.refresh(secrets, kubernetes, kubernetes, ARNS, now="2026-10-29T12:00:00Z", api_errors=(_ApiError,))
    assert kubernetes.secret_patches == [] and kubernetes.deployment_patches == []
    assert MARKER not in str(raised.value) and MARKER not in caplog.text


def test_kubernetes_request_errors_are_bounded(clients, caplog):
    secrets, kubernetes = clients

    def rejected(name: str, namespace: str, body: dict[str, Any]) -> None:
        raise _ApiError(f"synthetic rejection {MARKER}")

    kubernetes.patch_namespaced_secret = rejected
    with pytest.raises(refresh.RefreshError) as raised:
        refresh.refresh(secrets, kubernetes, kubernetes, ARNS, now="2026-10-29T12:00:00Z", api_errors=(_ApiError,))
    assert raised.value.__cause__ is None
    assert MARKER not in str(raised.value) and MARKER not in caplog.text


def test_reference_contract_is_closed():
    with pytest.raises(refresh.RefreshError):
        refresh.refresh(
            _Secrets(), _Kubernetes(), _Kubernetes(), {"runtime": ARNS["runtime"]}, now="x", api_errors=(_ApiError,)
        )
    with pytest.raises(refresh.RefreshError):
        refresh.parse_references(json.dumps({**ARNS, "extra": ARNS["runtime"]}))
    assert refresh.parse_references(json.dumps(ARNS)) == ARNS


CHART = Path(__file__).resolve().parents[5] / "platform" / "charts" / "shifter"
REFRESH_IDENTITY = "system:serviceaccount:shifter-platform:openfga-credential-refresh"
REFRESH_POLICY = "restrict-openfga-credential-refresh"


@cache
def _rendered() -> tuple[dict[str, Any], ...]:
    """Render the AWS chart with the password datastore and its refresh Job."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is required to evaluate the rendered admission policy")
    values = {
        "runtimeEnv": {"AWS_REGION": "us-east-2"},
        "identity": {"serviceAccountRoleArns": {"policyCredentialRefresh": "arn:aws:iam::123456789012:role/refresh"}},
        "network": {"kubernetesApiCidrs": ["10.42.0.0/24"]},
        "openfga": {
            "enabled": True,
            "storeId": "01J00000000000000000000001",
            "modelId": "01J00000000000000000000000",
            "databaseCidrs": ["10.0.0.0/16"],
            "runtimeDatabaseUser": "openfga_runtime",
            "adminDatabaseUser": "openfga_admin",
            "migrationDatabaseUser": "openfga_migrator",
            "passwordDatastore": {
                "enabled": True,
                "host": "db.example.test",
                "caBundleB64": base64.b64encode(b"synthetic-ca").decode(),
                "secretArns": ARNS,
            },
        },
    }
    overrides = [argument for key, value in values.items() for argument in ("--set-json", f"{key}={json.dumps(value)}")]
    rendered = subprocess.run(  # noqa: S603
        [helm, "template", "shifter", str(CHART), "-f", str(CHART / "values-aws-dev.yaml"), *overrides],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(item for item in yaml.safe_load_all(rendered.stdout) if isinstance(item, dict))


def _document(kind: str, name: str) -> dict[str, Any]:
    return deepcopy(next(item for item in _rendered() if item["kind"] == kind and item["metadata"]["name"] == name))


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a map-only merge patch, as the API server does for this body."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target


def _admitted(old: dict[str, Any], new: dict[str, Any], username: str = REFRESH_IDENTITY) -> bool:
    """Evaluate the rendered policy for one Deployment UPDATE."""
    policy = _document("ValidatingAdmissionPolicy", REFRESH_POLICY)
    environment = Environment()
    activation = {
        "object": json_to_cel(new),
        "oldObject": json_to_cel(old),
        "request": json_to_cel({"operation": "UPDATE", "userInfo": {"username": username}}),
    }
    for condition in policy["spec"]["matchConditions"]:
        program = environment.program(environment.compile(condition["expression"]))
        if program.evaluate(activation) != celtypes.BoolType(True):
            return True
    variables: dict[Any, Any] = {}
    try:
        for variable in policy["spec"]["variables"]:
            program = environment.program(environment.compile(variable["expression"]))
            variables[celtypes.StringType(variable["name"])] = program.evaluate(
                {**activation, "variables": celtypes.MapType(variables)}
            )
        return all(
            environment.program(environment.compile(rule["expression"])).evaluate(
                {**activation, "variables": celtypes.MapType(variables)}
            )
            == celtypes.BoolType(True)
            for rule in policy["spec"]["validations"]
        )
    except CELEvalError:
        return False


def _restart_patch() -> dict[str, Any]:
    secrets, kubernetes = _Secrets(), _Kubernetes()
    refresh.refresh(secrets, kubernetes, kubernetes, ARNS, now="2026-10-29T12:00:00Z", api_errors=(_ApiError,))
    return kubernetes.deployment_patches[0][1]


def test_refresh_restart_patch_is_admitted_for_both_policy_servers():
    assert _document("ValidatingAdmissionPolicy", REFRESH_POLICY)["spec"]["failurePolicy"] == "Fail"
    assert _document("ValidatingAdmissionPolicyBinding", REFRESH_POLICY)["spec"]["validationActions"] == ["Deny"]
    for server in ("openfga", "openfga-admin"):
        old = _document("Deployment", server)
        restarted = _merge(deepcopy(old), _restart_patch())
        assert _admitted(old, restarted)
        # The next weekly restart replaces the earlier annotation value.
        again = deepcopy(restarted)
        again["spec"]["template"]["metadata"]["annotations"][refresh.RESTART_ANNOTATION] = "2026-11-05T12:00:00Z"
        assert _admitted(restarted, again)


@pytest.mark.parametrize(
    "attack",
    ["service_account", "image", "command", "volumes", "env", "replicas", "pod_label", "other_annotation", "owner"],
)
def test_refresh_identity_cannot_change_anything_but_the_restart_annotation(attack):
    old = _document("Deployment", "openfga")
    new = _merge(deepcopy(old), _restart_patch())
    pod = new["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if attack == "service_account":
        pod["serviceAccountName"] = "openfga-migrator"
    elif attack == "image":
        container["image"] = "registry.example.test/attacker@sha256:" + "f" * 64
    elif attack == "command":
        container["command"] = ["/bin/sh", "-c", "env"]
    elif attack == "volumes":
        pod["volumes"].append({"name": "master", "secret": {"secretName": "openfga-migrator-datastore"}})
    elif attack == "env":
        container["env"].append({"name": "SYNTHETIC", "value": "1"})
    elif attack == "replicas":
        new["spec"]["replicas"] = 0
    elif attack == "pod_label":
        new["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] = "openfga-admin"
    elif attack == "other_annotation":
        new["spec"]["template"]["metadata"]["annotations"]["synthetic.example/extra"] = "1"
    else:
        new["metadata"].setdefault("annotations", {})["meta.helm.sh/release-name"] = "other"
    assert not _admitted(old, new)
    # Updates from other identities, such as the Deployment controller, are outside this policy.
    assert _admitted(old, new, username="system:serviceaccount:kube-system:deployment-controller")
