"""AWS OpenFGA password datastore and weekly credential refresh (#2313)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]
CA = "-----BEGIN CERTIFICATE-----\nU3ludGhldGljIFJEUyBDQQ==\n-----END CERTIFICATE-----\n"
ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks"
HOST = "dev-portal-db.example.us-east-2.rds.amazonaws.com"


def _values(**password: object) -> dict[str, object]:
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
            "storeId": "01J00000000000000000000001",
            "modelId": "01J00000000000000000000000",
            "databaseCidrs": ["10.0.0.0/16"],
            "runtimeDatabaseUser": "openfga_runtime",
            "adminDatabaseUser": "openfga_admin",
            "migrationDatabaseUser": "openfga_migrator",
            "passwordDatastore": {
                "enabled": True,
                "host": HOST,
                "caBundleB64": base64.b64encode(CA.encode()).decode(),
                "secretArns": {duty: f"{ARN}/openfga-{duty}-datastore" for duty in ("runtime", "admin", "migrator")},
                **password,
            },
        },
    }


def _render(tmp_path: Path, values: dict[str, object]) -> subprocess.CompletedProcess[str]:
    generated = tmp_path / "values.json"
    generated.write_text(json.dumps(values))
    return subprocess.run(
        ["helm", "template", "t", str(CHART), "-f", str(CHART / "values-aws-dev.yaml"), "-f", str(generated)],
        check=False,
        capture_output=True,
        text=True,
    )


def _documents(tmp_path: Path, values: dict[str, object]) -> dict[tuple[str, str], dict]:
    result = _render(tmp_path, values)
    assert result.returncode == 0, result.stderr
    return {(item["kind"], item["metadata"]["name"]): item for item in yaml.safe_load_all(result.stdout) if item}


def test_servers_use_named_login_secrets_and_verified_tls(tmp_path) -> None:
    documents = _documents(tmp_path, _values())
    for server, duty in (("openfga", "runtime"), ("openfga-admin", "admin")):
        pod = documents["Deployment", server]["spec"]["template"]["spec"]
        env = {item["name"]: item for item in pod["containers"][0]["env"]}
        uri = env["OPENFGA_DATASTORE_URI"]["value"]
        assert uri == f"postgres://{HOST}:5432/openfga?sslmode=verify-full&sslrootcert=/etc/openfga/rds-ca/ca.pem"
        assert "@" not in uri
        for name, key in (("OPENFGA_DATASTORE_USERNAME", "username"), ("OPENFGA_DATASTORE_PASSWORD", "password")):
            assert env[name]["valueFrom"]["secretKeyRef"] == {"name": f"openfga-{duty}-datastore", "key": key}
        assert pod["serviceAccountName"] in {"openfga-runtime", "openfga-admin"}
        assert {"name": "rds-ca", "configMap": {"name": "openfga-rds-ca"}} in pod["volumes"]
    assert documents["ConfigMap", "openfga-rds-ca"]["data"]["ca.pem"].strip() == CA.strip()
    job = documents["Job", "openfga-datastore-migrate"]["spec"]["template"]["spec"]
    psql = {item["name"]: item for item in job["containers"][0]["env"]}
    assert psql["PGSSLMODE"]["value"] == "verify-full"
    assert psql["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "openfga-migrator-datastore"
    assert "PGSERVICE" not in psql


def test_refresh_job_may_patch_only_named_secrets_and_policy_servers(tmp_path) -> None:
    documents = _documents(tmp_path, _values())
    role = documents["Role", "openfga-credential-refresh"]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["openfga-runtime-datastore", "openfga-admin-datastore", "openfga-migrator-datastore"],
            "verbs": ["patch"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["openfga", "openfga-admin"],
            "verbs": ["patch"],
        },
    ]
    account = documents["ServiceAccount", "openfga-credential-refresh"]
    assert account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"].endswith("policyCredentialRefresh")
    cron = documents["CronJob", "openfga-credential-refresh"]
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python", "-m", "shared.cloud.policy_credential_refresh"]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert json.loads(env["OPENFGA_DATASTORE_LOGIN_ARNS"])["runtime"].endswith("openfga-runtime-datastore")


def test_password_datastore_refuses_incomplete_settings(tmp_path) -> None:
    for change in ({"host": ""}, {"caBundleB64": ""}):
        result = _render(tmp_path, _values(**change))
        assert result.returncode != 0
        assert "password datastore requires" in result.stderr
    disabled = _values()
    disabled["openfga"]["passwordDatastore"]["enabled"] = False  # type: ignore[index]
    documents = _documents(tmp_path, disabled)
    assert ("CronJob", "openfga-credential-refresh") not in documents
    assert ("ConfigMap", "openfga-rds-ca") not in documents
