"""AWS workloads connect with their own RDS IAM login in every policy state (#2313)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1]
LOGINS = {
    "portal": "portal_web",
    "workers": "portal_workers",
    "ctf-scheduler": "ctf_scheduler",
    "provisioner-launcher": "provisioner_launcher",
}
# Every Deployment that runs the platform image and reads the application database.
DATABASE_WORKLOADS = {
    "portal-web",
    "ctf-communication-worker",
    "ctf-scheduler",
    "guacamole-bootstrap-prune",
    "raes-operation-record-prune",
    "worker-cms",
    "worker-engine",
    "worker-mc",
    "worker-operation-result-applier",
    "worker-outbox-drainer",
    "worker-pack-removal-reconciler",
    "worker-provisioner-launcher",
    "worker-reconciler",
    "worker-warm-pool-reconciler",
}
ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks"


def _openfga(store_id: str, model_id: str) -> dict[str, object]:
    return {
        "runtimeEnv": {"AWS_REGION": "us-east-2"},
        "identity": {
            "serviceAccountRoleArns": {
                "policyCredentialRefresh": "arn:aws:iam::123456789012:role/shifter-dev-eks-policyCredentialRefresh"
            }
        },
        "network": {"kubernetesApiCidrs": ["10.42.0.0/24"]},
        "openfga": {
            "enabled": True,
            "storeId": store_id,
            "modelId": model_id,
            "databaseCidrs": ["10.0.0.0/16"],
            "runtimeDatabaseUser": "openfga_runtime",
            "adminDatabaseUser": "openfga_admin",
            "migrationDatabaseUser": "openfga_migrator",
            "passwordDatastore": {
                "enabled": True,
                "host": "dev-portal-db.example.us-east-2.rds.amazonaws.com",
                "caBundleB64": base64.b64encode(b"synthetic-ca").decode(),
                "secretArns": {duty: f"{ARN}/openfga-{duty}-datastore" for duty in ("runtime", "admin", "migrator")},
            },
        },
    }


POLICY_STATES = {
    "openfga-disabled": {},
    # The documented first publication: servers run, the portal client stays unbound.
    "openfga-enabled-unbound": _openfga("", ""),
    "openfga-bound": _openfga("01J00000000000000000000001", "01J00000000000000000000000"),
}


def _database_env(tmp_path: Path, values: dict[str, object]) -> dict[str, dict[str, str | None]]:
    generated = tmp_path / "values.json"
    generated.write_text(json.dumps({**values, "rdsIam": {"enabled": True, "databaseUsers": LOGINS}}))
    result = subprocess.run(
        ["helm", "template", "t", str(CHART), "-f", str(CHART / "values-aws-dev.yaml"), "-f", str(generated)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    workloads: dict[str, dict[str, str | None]] = {}
    for document in yaml.safe_load_all(result.stdout):
        if not document or document["kind"] != "Deployment":
            continue
        name = document["metadata"]["name"]
        if name in DATABASE_WORKLOADS:
            container = document["spec"]["template"]["spec"]["containers"][0]
            workloads[name] = {item["name"]: item.get("value") for item in container.get("env") or []}
    return workloads


@pytest.mark.parametrize("state", sorted(POLICY_STATES))
def test_every_database_workload_uses_its_own_rds_iam_login(tmp_path, state) -> None:
    workloads = _database_env(tmp_path, POLICY_STATES[state])
    assert set(workloads) == DATABASE_WORKLOADS
    for name, env in workloads.items():
        assert env.get("DB_AUTH_MODE") == "rds-iam", name
        assert env.get("DB_USER") in set(LOGINS.values()), name
        # A runtime workload never reads the master credential.
        assert env.get("DB_SECRET_ID") == "", name
    assert workloads["portal-web"]["DB_USER"] == "portal_web"
    assert workloads["ctf-scheduler"]["DB_USER"] == "ctf_scheduler"
    assert workloads["worker-provisioner-launcher"]["DB_USER"] == "provisioner_launcher"
    # The policy client binds only when both identifiers exist.
    bound = state == "openfga-bound"
    assert ("OPENFGA_STORE_ID" in workloads["portal-web"]) is bound
