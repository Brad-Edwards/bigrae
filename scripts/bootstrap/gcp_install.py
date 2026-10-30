"""``gcp-install``: stand up a GCP deployment from a release and one bundle (#2679).

An operator who has only a published release and a deployment input bundle
(``bigrae.yaml`` and the Terraform values) runs one command. It:

1. validates the bundle;
2. applies the GitHub-less foundation, with inputs derived from the bundle and
   the operator's own Google identity as the only impersonation principal;
3. creates the initial operator password secret when it does not exist;
4. publishes the bundle to the deployment config bucket;
5. runs ``gcp-deploy --first-install``, which imports the release's base range
   images, generates the OpenFGA credentials, publishes the authorization
   model, creates the first administrator, and registers the in-box catalog.

Each step is idempotent, so the operator runs the same command again after a
failure. After DNS points at the printed ingress address, ``gcp-deploy`` waits
for the managed certificate and completes the install.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bootstrap_core import header, info, success
from gcp_config_store import publish
from gcp_deploy import DeployOptions, GcpDeployError, run_deploy
from gcp_foundation import bootstrap_gcp_foundation
from gcp_inputs import DeploymentInputs, load_deployment_inputs, operator_password_reference, write_private_file
from gcp_process import ProcessRunner

_PHRASE_CHARACTERS = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True)
class InstallOptions:
    """Parsed options of ``gcp-install``."""

    inputs: str
    release_manifest: str = ""
    release_source: str = ""
    operator: str = ""


def operator_principal(runner: ProcessRunner, explicit: str) -> str:
    """Return the IAM member of the operator: the explicit one, or the active gcloud account."""
    account = explicit or runner.run(["gcloud", "config", "get-value", "account"]).stdout.strip()
    if not account or "@" not in account:
        raise GcpDeployError("No active gcloud account; run gcloud auth login or pass --operator")
    if account.startswith(("user:", "group:", "serviceAccount:")):
        return account
    return f"serviceAccount:{account}" if account.endswith(".gserviceaccount.com") else f"user:{account}"


def foundation_inputs(inputs: DeploymentInputs, *, project_number: str, principal: str) -> dict[str, object]:
    """Derive the GitHub-less foundation inputs from the bundle and the operator."""
    project = inputs.project_id
    return {
        "project_id": project,
        "project_number": project_number,
        "environment": inputs.environment,
        "name_prefix": f"shifter-{inputs.environment}",
        "impersonation_principals": {purpose: [principal] for purpose in ("deploy", "release_scan", "destroy")},
        "deployment_config_bucket_name": f"{project}-deployment-config",
        "terraform_state_bucket_name": f"{project}-terraform-state",
        "release_evidence_bucket_name": f"{project}-release-evidence",
        "region": str(inputs.settings.region),  # type: ignore[attr-defined]
        "enable_image_build_network": False,
    }


def ensure_operator_password(runner: ProcessRunner, inputs: DeploymentInputs) -> bool:
    """Create the initial operator password secret when it is missing. Return True when created."""
    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    if operator is None or not operator.enabled:
        raise GcpDeployError("gcp-install needs settings.operator_bootstrap.admin_email for the first administrator")
    _, project, _, name = operator_password_reference(inputs).split("/", 3)
    runner.run(["gcloud", "services", "enable", "secretmanager.googleapis.com", "--project", project])
    exists = runner.run(
        ["gcloud", "secrets", "versions", "describe", "latest", "--secret", name, "--project", project], check=False
    )
    if exists.returncode == 0:
        return False
    password = "-".join("".join(secrets.choice(_PHRASE_CHARACTERS) for _ in range(6)) for _ in range(4))
    runner.run(
        ["gcloud", "secrets", "create", name, "--project", project, "--replication-policy=automatic", "--data-file=-"],
        input_text=password,
        sensitive=True,
    )
    info(
        "Created the initial operator password. Read it with: "
        f"gcloud secrets versions access latest --secret {name} --project {project}"
    )
    return True


def run_install(options: InstallOptions, *, runner: ProcessRunner | None = None) -> int:
    """Run ``gcp-install``. Return the ``gcp-deploy`` exit status."""
    runner = runner or ProcessRunner()
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-install-"))
    try:
        inputs = load_deployment_inputs(options.inputs, workdir / "bundle", runner, {})
        header(f"GCP install: {inputs.environment}")
        principal = operator_principal(runner, options.operator)
        number = runner.run(
            ["gcloud", "projects", "describe", inputs.project_id, "--format=value(projectNumber)"]
        ).stdout.strip()
        path = write_private_file(
            workdir / "foundation.json",
            json.dumps(foundation_inputs(inputs, project_number=number, principal=principal), indent=2),
        )
        bootstrap_gcp_foundation(str(path))
        ensure_operator_password(runner, inputs)
        store = f"gs://{inputs.project_id}-deployment-config"
        publish(store, options.inputs, runner=runner)
        success(f"Published the deployment inputs to {store}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return run_deploy(
        DeployOptions(
            source=store,
            release_manifest=options.release_manifest,
            release_source=options.release_source,
            first_install=True,
        ),
        runner=runner,
    )
