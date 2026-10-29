"""Semantic admission tests for the GCP native database proxy boundary."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.platform.test_gcp_job_launcher_manifests import (
    PROVISIONER_LAUNCHER_USERNAME,
    _canonical_admission_job,
    _canonical_admission_parameters,
    _load_helm_documents,
    _policy,
    _semantic_policy_allows,
)


def _native_database_admission_job() -> tuple[dict[str, Any], dict[str, str]]:
    job = _canonical_admission_job()
    pod = job["spec"]["template"]["spec"]
    env = pod["containers"][0]["env"]
    env[:] = [entry for entry in env if entry["name"] != "DB_PASSWORD"]
    for entry in env:
        if entry["name"] == "DB_HOST":
            entry["value"] = "127.0.0.1"
        if entry["name"] == "DB_USER":
            entry["value"] = "provisioner@synthetic-project.iam"
    env.append({"name": "DB_AUTH_MODE", "value": "cloud-sql-proxy"})
    instance = "synthetic-project:us-central1:platform-pg"
    pod["initContainers"] = [
        {
            "name": "cloud-sql-proxy",
            "image": (
                "gcr.io/cloud-sql-connectors/cloud-sql-proxy@sha256:"
                "4f9071e7fb8bc0acc0c66dbbaa292d7c0e6337003ccd29f75e1431f8f8c3fce6"
            ),
            "imagePullPolicy": "IfNotPresent",
            "restartPolicy": "Always",
            "args": [
                "--auto-iam-authn",
                "--private-ip",
                "--address=127.0.0.1",
                "--port=5432",
                "--structured-logs",
                "--health-check",
                "--http-address=0.0.0.0",
                "--http-port=9801",
                "--run-connection-test",
                instance,
            ],
            "securityContext": {
                "runAsUser": 65532,
                "runAsGroup": 65532,
                "runAsNonRoot": True,
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "16Mi"},
                "limits": {"cpu": "1", "memory": "256Mi", "ephemeral-storage": "32Mi"},
            },
            "startupProbe": {
                "httpGet": {"path": "/startup", "port": 9801},
                "periodSeconds": 1,
                "timeoutSeconds": 1,
                "failureThreshold": 60,
            },
        }
    ]
    parameters = _canonical_admission_parameters()
    parameters.update(
        DB_HOST="127.0.0.1", PROVISIONER_DB_USER="provisioner@synthetic-project.iam", DB_AUTH_MODE="cloud-sql-proxy"
    )
    parameters["CLOUD_SQL_INSTANCE_CONNECTION_NAME"] = instance
    return job, parameters


@pytest.mark.parametrize("inline_params", [True, False])
def test_native_database_admission_accepts_only_the_exact_host_selected_sidecar(inline_params):
    canonical, parameters = _native_database_admission_job()
    policy = _policy(_load_helm_documents(runtime_env=parameters, inline_params=inline_params))
    assert _semantic_policy_allows(policy, PROVISIONER_LAUNCHER_USERNAME, canonical, parameter_data=parameters)
    for mutation in (
        lambda p: p.pop("initContainers"),
        lambda p: p["initContainers"].append(copy.deepcopy(p["initContainers"][0])),
        lambda p: p["initContainers"][0].update(image="untrusted/proxy:latest"),
        lambda p: p["initContainers"][0].update(command=["sh", "-c", "true"]),
        lambda p: p["initContainers"][0].update(
            env=[{"name": "GOOGLE_APPLICATION_CREDENTIALS", "value": "/credential/key"}]
        ),
        lambda p: p["initContainers"][0].update(volumeMounts=[{"name": "credential", "mountPath": "/credential"}]),
        lambda p: p["initContainers"][0]["args"].append("--impersonate-service-account=operator"),
        lambda p: p["initContainers"][0]["args"].__setitem__(2, "--address=0.0.0.0"),
        lambda p: p["initContainers"][0]["args"].__setitem__(-1, "other-project:us-central1:other-db"),
        lambda p: p["initContainers"][0]["securityContext"].update(runAsUser=0),
        lambda p: p["initContainers"][0]["startupProbe"].update(exec={"command": ["true"]}),
        lambda p: p["initContainers"][0].update(restartPolicy="Never"),
        lambda p: p["containers"][0]["env"].append(
            {
                "name": "DB_PASSWORD",
                "valueFrom": {
                    "secretKeyRef": {"name": "pulumi-provisioner-secrets-0123456789abcdef", "key": "DB_PASSWORD"}
                },
            }
        ),
    ):
        altered = copy.deepcopy(canonical)
        mutation(altered["spec"]["template"]["spec"])
        assert not _semantic_policy_allows(policy, PROVISIONER_LAUNCHER_USERNAME, altered, parameter_data=parameters)
    disabled = {**parameters, "DB_AUTH_MODE": "password"}
    disabled_policy = _policy(_load_helm_documents(runtime_env=disabled, inline_params=inline_params))
    assert not _semantic_policy_allows(
        disabled_policy, PROVISIONER_LAUNCHER_USERNAME, canonical, parameter_data=disabled
    )
