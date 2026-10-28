"""Runtime environment composition for the GCP Helm release (#2571).

The platform runtime ConfigMap is one projection of validated inputs:

1. the bootstrap-owned static contract (``render_gcp_platform_runtime_env``);
2. the GCP runtime renderer (``scripts/gcp/render_runtime_env.py``) over the Terraform
   outputs, run in a clean environment that holds only the deployment projections:
   range-cell settings, the participant OpenVPN opt-in, the operator elevation lists,
   the range image references, and the range-host identity from Terraform.

The renderer never sees the operator's process environment, so no ambient variable can
change the runtime contract (root-configured backend bundles, "Runtime Binding").
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from bootstrap_core import GDCBootstrapConfig, get_repo_root
from gcp_control_plane import parse_env_contract, render_gcp_platform_runtime_env
from gcp_inputs import DeploymentInputs, private_workdir, write_private_file
from gcp_process import ProcessRunner, isolated_env, minimal_env, script_argv


def gdc_config(inputs: DeploymentInputs) -> GDCBootstrapConfig:
    """Return the bootstrap config view of a deployment (project, environment, region, backend)."""
    settings = inputs.settings
    zone = settings.range_cells.zone or f"{inputs.region}-a"  # type: ignore[attr-defined]
    return GDCBootstrapConfig(
        project_id=inputs.project_id,
        environment=inputs.environment,
        region=inputs.region,
        zone=zone,
        range_backend=settings.range_cells.backend,  # type: ignore[attr-defined]
        shifter_config_path=str(inputs.config_path),
    )


def renderer_inputs(inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    """Return the exact process inputs the runtime renderer reads for this deployment."""
    from installation.settings_gcp_deploy import deployment_runtime_env

    settings = inputs.settings
    values = deployment_runtime_env(
        settings.range_cells,  # type: ignore[attr-defined]
        settings.participant_openvpn,  # type: ignore[attr-defined]
        settings.operator_bootstrap,  # type: ignore[attr-defined]
    )
    values.update(inputs.range_image_env())
    host_identity = outputs.get("range_host_service_account_email", {}).get("value")
    if isinstance(host_identity, str) and host_identity.strip():
        values["GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL"] = host_identity.strip()
    return values


def compose_runtime_env(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Mapping[str, object]],
    *,
    provisioner_digest: str,
    workdir: Path,
) -> dict[str, str]:
    """Render the full runtime env for the Helm release."""
    directory = private_workdir(workdir / "runtime")
    outputs_file = write_private_file(directory / "terraform-outputs.json", json.dumps(outputs))
    rendered_file = directory / "platform-runtime.generated.env"
    rendered_file.unlink(missing_ok=True)
    env = {**minimal_env(runner.base_env), **renderer_inputs(inputs, outputs)}
    runner.run(
        script_argv(
            get_repo_root(),
            "render_runtime_env",
            "--terraform-output-json",
            str(outputs_file),
            "--engine-image-digest",
            provisioner_digest,
            "--output",
            str(rendered_file),
        ),
        env=isolated_env(runner.base_env, env),
    )
    static = parse_env_contract(render_gcp_platform_runtime_env(gdc_config(inputs)))
    rendered = parse_env_contract(rendered_file.read_text(encoding="utf-8"))
    return {**static, **rendered}
