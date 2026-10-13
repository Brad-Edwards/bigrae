"""Validate native identity readback and project public connection metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool

NATIVE_APPLICATION_WORKLOADS = frozenset({"portal", "workers", "ctf-scheduler", "provisioner-launcher", "provisioner"})
NATIVE_POLICY_WORKLOADS = frozenset({"openfga-runtime", "openfga-admin", "openfga-migrator"})
CLOUD_SQL_PROXY_IMAGE = (
    "gcr.io/cloud-sql-connectors/cloud-sql-proxy@sha256:"
    "4f9071e7fb8bc0acc0c66dbbaa292d7c0e6337003ccd29f75e1431f8f8c3fce6"
)


class GcpNativeIdentityReadback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    openfga_enabled: StrictBool = False
    instance_connection_name: str = ""
    database_users: dict[str, str] = Field(default_factory=dict)


def validate_native_identity(
    raw: object,
    *,
    project_id: str,
    service_accounts: object,
) -> GcpNativeIdentityReadback:
    """Reject mismatched deployment identities before any manifest publication."""
    native = GcpNativeIdentityReadback.model_validate(raw or {})
    if not native.enabled:
        if native.openfga_enabled or native.database_users:
            raise ValueError("native identity readback has active duties while disabled")
        return native
    expected = NATIVE_APPLICATION_WORKLOADS | (NATIVE_POLICY_WORKLOADS if native.openfga_enabled else frozenset())
    instance = native.instance_connection_name.split(":")
    if (
        set(native.database_users) != expected
        or len(instance) != 3
        or instance[0] != project_id
        or not all(instance)
        or len(set(native.database_users.values())) != len(expected)
        or not isinstance(service_accounts, Mapping)
    ):
        raise ValueError("native identity readback has incomplete or mismatched database duties")
    if not re.fullmatch(
        r"[a-z][a-z0-9-]{4,28}[a-z0-9]:[a-z][a-z0-9-]+:[a-z][a-z0-9-]{0,97}", native.instance_connection_name
    ):
        raise ValueError("native identity readback has an invalid instance connection name")
    for workload, user in native.database_users.items():
        email = service_accounts.get(workload)
        if (
            not isinstance(email, str)
            or not email.endswith(f"@{project_id}.iam.gserviceaccount.com")
            or user != email.removesuffix(".gserviceaccount.com")
            or not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam", user)
        ):
            raise ValueError("native database login does not match its attached workload identity")
    return native


def cloud_sql_proxy_spec(instance: str, *, auto_iam: bool = True) -> dict:
    """The approved released native sidecar; no credential or command inputs."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]:[a-z][a-z0-9-]+:[a-z][a-z0-9-]{0,97}", instance):
        raise ValueError("Native Cloud SQL proxy requires a valid instance connection name")
    return {
        "name": "cloud-sql-proxy",
        "image": CLOUD_SQL_PROXY_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "restartPolicy": "Always",
        "args": (["--auto-iam-authn"] if auto_iam else [])
        + [
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
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
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
