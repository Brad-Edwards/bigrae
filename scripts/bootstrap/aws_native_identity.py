"""AWS native identity deployment steps for the EKS lifecycle (#2313).

These helpers keep the identity-specific parts of ``aws_eks`` in one place:

* project the service proof, gateway, SQL-login and policy values;
* publish the operator policy bundles and the datastore login Secrets.

Values move to kubectl only through stdin. No credential reaches argv, a
temporary file, Helm values or the deploy log.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping

from installation.openfga_bundles import policy_documents
from installation.settings_aws import AwsOpenFgaSettings

OPENFGA_DUTIES = ("runtime", "admin", "migrator")
# Chart database roles (shifter.databaseUserEnv) keyed by Terraform role key.
_CHART_DATABASE_ROLES = {
    "portal": "portal",
    "workers": "workers",
    "ctfScheduler": "ctf-scheduler",
    "provisionerLauncher": "provisioner-launcher",
}
_GATEWAY_CONTROL_PATH = "/api/v1/mission-control/guacamole/control/authority/"


def _output(outputs: Mapping[str, object], name: str) -> object:
    raw = outputs.get(name)
    if not isinstance(raw, Mapping) or "value" not in raw:
        raise ValueError(f"missing required EKS Terraform output {name!r}")
    return raw["value"]


def _string_output(outputs: Mapping[str, object], name: str) -> str:
    value = _output(outputs, name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"EKS Terraform output {name!r} must be a non-empty string")
    return value


def datastore_secret_arns(outputs: Mapping[str, object]) -> dict[str, str]:
    """Return the closed OpenFGA datastore secret ARN map."""
    arns = _output(outputs, "openfga_datastore_secret_arns")
    if not isinstance(arns, Mapping) or set(arns) != set(OPENFGA_DUTIES):
        raise ValueError("openfga_datastore_secret_arns must map runtime, admin and migrator")
    return {duty: str(arns[duty]) for duty in OPENFGA_DUTIES}


def database_iam_users(outputs: Mapping[str, object]) -> dict[str, str]:
    """Return the chart role to RDS IAM login map from Terraform."""
    users = _output(outputs, "database_iam_users")
    if not isinstance(users, Mapping) or set(users) != set(_CHART_DATABASE_ROLES):
        raise ValueError("database_iam_users must name the four runtime workload logins")
    return {_CHART_DATABASE_ROLES[key]: str(login) for key, login in users.items()}


def runtime_env(
    outputs: Mapping[str, object], roles: Mapping[str, str], *, domain: str, ca_bundle: bytes
) -> dict[str, str]:
    """Return the renderer-owned AWS identity runtime bindings."""
    return {
        "AWS_ACCOUNT_ID": _string_output(outputs, "aws_account_id"),
        "AWS_SERVICE_TOKEN_AUDIENCE": f"https://{domain}",
        "AWS_SERVICE_TOKEN_ISSUER": _string_output(outputs, "service_token_issuer"),
        "DB_SSLMODE": "verify-full",
        "DB_SSL_ROOT_CERT_PEM_B64": base64.b64encode(ca_bundle).decode("ascii"),
        "GUACAMOLE_CONTROL_AUDIENCE": _string_output(outputs, "gateway_control_audience"),
        "GUACAMOLE_CONTROL_IDENTITY": "aws",
        "GUACAMOLE_CONTROL_URL": f"https://{domain}{_GATEWAY_CONTROL_PATH}",
        "GUACAMOLE_CONTROLLER_ROLE_ARN": roles["guacamoleClient"],
    }


def openfga_settings(settings: Mapping[str, object]) -> AwsOpenFgaSettings:
    """Return the validated operator OpenFGA references."""
    return AwsOpenFgaSettings.model_validate(settings.get("openfga") or {})


def chart_values(
    settings: Mapping[str, object],
    outputs: Mapping[str, object],
    *,
    db_host: str,
    database_cidrs: list[str],
    ca_bundle: bytes,
) -> dict[str, object]:
    """Return the chart values for per-workload SQL logins and the policy service."""
    policy = openfga_settings(settings)
    return {
        "rdsIam": {"enabled": True, "databaseUsers": database_iam_users(outputs)},
        "openfga": {
            "enabled": policy.enabled,
            "storeId": policy.store_id,
            "modelId": policy.model_id,
            "runtimeDatabaseUser": "openfga_runtime",
            "adminDatabaseUser": "openfga_admin",
            "migrationDatabaseUser": "openfga_migrator",
            "databaseCidrs": database_cidrs,
            "passwordDatastore": {
                "enabled": policy.enabled,
                "host": db_host,
                "caBundleB64": base64.b64encode(ca_bundle).decode("ascii"),
                "secretArns": datastore_secret_arns(outputs),
            },
        },
    }


def _datastore_document(duty: str, payload: str) -> dict[str, object]:
    credential = json.loads(payload)
    username, password = credential.get("username"), credential.get("password")
    if (
        not isinstance(username, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", username)
        or not isinstance(password, str)
        or not password
    ):
        raise ValueError("OpenFGA datastore credential has an unexpected shape")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"openfga-{duty}-datastore",
            "namespace": "shifter-platform",
            "labels": {"app.kubernetes.io/part-of": "shifter"},
        },
        "type": "Opaque",
        "stringData": {"username": username, "password": password},
    }


def publish_policy_credentials(
    settings: Mapping[str, object],
    outputs: Mapping[str, object],
    *,
    read_secret: Callable[[str], str],
    apply: Callable[[dict[str, object]], None],
) -> bool:
    """Publish the operator policy bundles and current datastore logins; False when disabled."""
    policy = openfga_settings(settings)
    if not policy.enabled:
        return False
    assert policy.runtime_secret_ref and policy.admin_secret_ref and policy.ca_secret_ref
    documents = policy_documents(
        json.loads(read_secret(policy.runtime_secret_ref)),
        json.loads(read_secret(policy.admin_secret_ref)),
        json.loads(read_secret(policy.ca_secret_ref)),
    )
    documents += [_datastore_document(duty, read_secret(arn)) for duty, arn in datastore_secret_arns(outputs).items()]
    for document in documents:
        apply(document)
    return True
