"""Platform Terraform for the GitHub-less GCP deploy commands (#2571, ADR-070).

The command runs the shared platform root (``platform/terraform/gcp/environments/
platform``) with the deployment's own state bucket and prefix. It writes these var files into a
private directory, in Terraform precedence order:

1. ``terraform.tfvars`` and then ``terraform.tfvars.json``: the operator's Terraform values;
2. ``settings.auto.tfvars``: the root-config bridge (range egress, dynamic-secret
   project, telemetry, broker, capacity) from ``installation.render.render_tfvars``;
3. ``zz-deployment.auto.tfvars.json``: the values that the root config owns (project,
   region, environment, public hostname, managed TLS, and the deployment settings).

The command-owned values come last, so they win over a conflicting operator value. The
first operator's credentials never enter Terraform; the bootstrap script consumes them.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bootstrap_core import (
    get_repo_root,
    info,
    validate_gcp_control_plane_security_inputs,
    warn,
)
from gcp_inputs import DeploymentInputs, private_workdir, write_private_file
from gcp_process import GcpCommandError, ProcessRunner

IDENTITY_PLATFORM_CONFIG_ADDRESS = (
    "module.platform_core.module.portal_identity_platform.google_identity_platform_config.platform"
)
SQL_INSTANCE_SUFFIX = "google_sql_database_instance.platform"
KMS_KEY_SUFFIXES = ("google_kms_crypto_key.artifact_registry", "google_kms_key_ring.artifact_registry")
COMMAND_OWNED_KEYS = frozenset(
    {
        "project_id",
        "region",
        "artifact_registry_location",
        "environment",
        "public_hostname",
        "enable_managed_tls",
        "openvpn_pool_enabled",
        "range_host_identity_pool_size",
        "range_network_zones",
        "ctf_content_bucket_name",
        "cloud_sql_deletion_protection",
        "gcs_force_destroy",
        "gcp_bootstrap_admin_email",
        "gcp_bootstrap_admin_password",
    }
)
_RETRYABLE_PERMISSION_MARKERS = (
    "permission denied",
    "iam_permission_denied",
    "does not have",
    "error 403",
    " denied on resource ",
)
_DESTROY_ATTEMPTS = 6


class TerraformStepError(RuntimeError):
    """A platform Terraform step failed."""


def state_bucket_name(project_id: str) -> str:
    """Return the deployment's Terraform state bucket."""
    return f"{project_id}-terraform-state"


def backend_prefix(environment: str) -> str:
    """Return the deployment's platform state prefix."""
    return f"shifter/{environment}/platform-core"


@dataclass(frozen=True)
class TerraformContext:
    """The root, metadata directory, var files, and environment of one Terraform run."""

    root: Path
    data_dir: Path
    var_files: tuple[Path, ...]
    env: dict[str, str | None] = field(default_factory=dict)

    def var_file_args(self) -> list[str]:
        """Return the ``-var-file`` arguments in precedence order."""
        return [f"-var-file={path}" for path in self.var_files]

    def argv(self, *args: str) -> list[str]:
        """Return a ``terraform -chdir=<root>`` command."""
        return ["terraform", f"-chdir={self.root}", *args]


def _hcl_assignment_names(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        head, separator, _ = line.partition("=")
        name = head.strip()
        if separator and name.isidentifier():
            names.add(name)
    return names


def owned_keys_in_values(path: Path) -> list[str]:
    """Return command-owned variable names that the operator values file also sets."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        names = set(json.loads(text))
    else:
        names = _hcl_assignment_names(text)
    return sorted(names & COMMAND_OWNED_KEYS)


def deployment_values(inputs: DeploymentInputs, *, destroy: bool = False) -> dict[str, object]:
    """Return the Terraform values that the root config owns."""
    from installation.settings_gcp_deploy import deployment_terraform_values

    settings = inputs.settings
    values: dict[str, object] = {
        "project_id": inputs.project_id,
        "region": inputs.region,
        "artifact_registry_location": inputs.region,
        "environment": inputs.environment,
        "public_hostname": inputs.public_hostname,
        "enable_managed_tls": True,
        **deployment_terraform_values(
            settings.range_cells,  # type: ignore[attr-defined]
            settings.participant_openvpn,  # type: ignore[attr-defined]
            settings.ctf_content_bucket,  # type: ignore[attr-defined]
        ),
    }
    if destroy:
        values["cloud_sql_deletion_protection"] = False
        values["gcs_force_destroy"] = True
    return values


def render_var_files(inputs: DeploymentInputs, workdir: Path, *, destroy: bool = False) -> tuple[Path, ...]:
    """Render the private var files and validate the control-plane security inputs."""
    from installation.render import render_tfvars

    directory = private_workdir(workdir / "terraform-vars")
    values_copies: list[Path] = []
    for values_path in inputs.terraform_values_paths:
        values_copy = directory / values_path.name
        values_copy.unlink(missing_ok=True)
        shutil.copyfile(values_path, values_copy)
        values_copy.chmod(0o600)
        values_copies.append(values_copy)
        overridden = owned_keys_in_values(values_copy)
        if overridden:
            warn(f"Root-config values override these {values_path.name} values: " + ", ".join(overridden))
    settings_file = write_private_file(directory / "settings.auto.tfvars", render_tfvars(inputs.root_config))  # type: ignore[arg-type]
    deployment_file = write_private_file(
        directory / "zz-deployment.auto.tfvars.json",
        json.dumps(deployment_values(inputs, destroy=destroy), indent=2, sort_keys=True) + "\n",
    )
    validate_gcp_control_plane_security_inputs(directory)
    return (*values_copies, settings_file, deployment_file)


def terraform_context(
    inputs: DeploymentInputs, workdir: Path, identity_env: Mapping[str, str | None], *, destroy: bool = False
) -> TerraformContext:
    """Prepare the var files and the environment for the platform root."""
    root = get_repo_root() / "platform" / "terraform" / "gcp" / "environments" / "platform"
    data_dir = private_workdir(workdir / "terraform-data")
    env: dict[str, str | None] = {
        **identity_env,
        "TF_DATA_DIR": str(data_dir),
        "TF_IN_AUTOMATION": "1",
        "TF_INPUT": "0",
    }
    return TerraformContext(
        root=root, data_dir=data_dir, var_files=render_var_files(inputs, workdir, destroy=destroy), env=env
    )


def terraform_init(runner: ProcessRunner, ctx: TerraformContext, *, state_bucket: str, prefix: str) -> None:
    """Initialize the platform root against the deployment state bucket and prefix."""
    runner.run(
        ctx.argv(
            "init",
            "-reconfigure",
            "-input=false",
            f"-backend-config=bucket={state_bucket}",
            f"-backend-config=prefix={prefix}",
        ),
        env=ctx.env,
        capture=False,
    )


def state_addresses(runner: ProcessRunner, ctx: TerraformContext) -> list[str]:
    """Return every resource address in the platform state."""
    result = runner.run(ctx.argv("state", "list"), env=ctx.env, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def adopt_identity_platform_config(runner: ProcessRunner, ctx: TerraformContext, *, project_id: str) -> None:
    """Import the initialized Identity Platform config singleton when state lacks it."""
    if IDENTITY_PLATFORM_CONFIG_ADDRESS in state_addresses(runner, ctx):
        info("Identity Platform config already managed by Terraform")
        return
    info("Importing the pre-provisioned Identity Platform config")
    runner.run(
        ctx.argv("import", "-input=false", *ctx.var_file_args(), IDENTITY_PLATFORM_CONFIG_ADDRESS, project_id),
        env=ctx.env,
        capture=False,
    )


def _is_permission_propagation(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RETRYABLE_PERMISSION_MARKERS)


def terraform_apply(
    runner: ProcessRunner,
    ctx: TerraformContext,
    *,
    max_attempts: int = 12,
    sleep_seconds: int = 10,
    sleep=time.sleep,
) -> None:
    """Apply the platform root, retrying only IAM propagation failures."""
    argv = ctx.argv("apply", "-auto-approve", "-input=false", *ctx.var_file_args())
    for attempt in range(1, max_attempts + 1):
        result = runner.run(argv, env=ctx.env, check=False)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode == 0:
            return
        combined = f"{result.stdout}\n{result.stderr}"
        if attempt < max_attempts and _is_permission_propagation(combined):
            warn(
                f"Terraform apply hit an IAM propagation error; retrying in {sleep_seconds}s ({attempt}/{max_attempts})"
            )
            sleep(sleep_seconds)
            continue
        if result.stderr:
            print(result.stderr, end="")
        raise TerraformStepError(f"terraform apply failed with exit status {result.returncode}")


def terraform_outputs(runner: ProcessRunner, ctx: TerraformContext) -> dict[str, dict[str, object]]:
    """Return the platform outputs as parsed JSON."""
    result = runner.run(ctx.argv("output", "-json"), env=ctx.env)
    try:
        outputs = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise TerraformStepError("terraform output did not return JSON") from None
    if not isinstance(outputs, dict):
        raise TerraformStepError("terraform output must be a JSON object")
    return outputs


def lift_sql_deletion_protection(runner: ProcessRunner, ctx: TerraformContext) -> None:
    """Apply the rendered ``cloud_sql_deletion_protection = false`` to the live instance."""
    matches = [address for address in state_addresses(runner, ctx) if address.endswith(SQL_INSTANCE_SUFFIX)]
    if not matches:
        info("Cloud SQL instance not in state; skipping the deletion-protection lift")
        return
    for address in matches:
        info(f"Lifting Cloud SQL deletion protection on {address}")
        runner.run(
            ctx.argv("apply", "-auto-approve", "-input=false", *ctx.var_file_args(), f"-target={address}"),
            env=ctx.env,
            capture=False,
        )


def drop_unremovable_kms_resources(runner: ProcessRunner, ctx: TerraformContext) -> None:
    """Remove the permanent Artifact Registry KMS key ring and key from state."""
    addresses = state_addresses(runner, ctx)
    for suffix in KMS_KEY_SUFFIXES:
        for address in [item for item in addresses if item.endswith(suffix)]:
            info(f"Dropping from state: {address}")
            runner.run(ctx.argv("state", "rm", address), env=ctx.env)


def terraform_destroy(runner: ProcessRunner, ctx: TerraformContext, *, sleep=time.sleep) -> None:
    """Destroy the platform root with bounded retries for provider-side convergence."""
    argv = ctx.argv("destroy", "-auto-approve", "-input=false", *ctx.var_file_args())
    for attempt in range(1, _DESTROY_ATTEMPTS + 1):
        try:
            runner.run(argv, env=ctx.env, capture=False)
            return
        except GcpCommandError:
            if attempt == _DESTROY_ATTEMPTS:
                raise TerraformStepError(f"terraform destroy failed after {_DESTROY_ATTEMPTS} attempts") from None
            delay = attempt * 60
            warn(f"Terraform destroy attempt {attempt} failed; retrying in {delay}s for GCP dependency convergence")
            sleep(delay)
