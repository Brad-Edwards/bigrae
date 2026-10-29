"""Stages of the GitHub-less GCP deploy command (#2571, ADR-070).

Each stage takes validated inputs and runs under one purpose identity that the caller
selects with :func:`gcp_process.scoped_environment`. The stages are the single deploy
implementation: the operator command runs them in order, and CI runs the same stages in
separate jobs.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Mapping
from dataclasses import dataclass

from bootstrap_core import get_repo_root, header, info, success, warn
from gcp_evidence import EvidenceRun
from gcp_inputs import DeploymentInputs
from gcp_kube import cluster_name, kube_env
from gcp_process import ProcessRunner, scoped_environment, script_argv


class DeployStageError(RuntimeError):
    """A deploy stage failed."""


@dataclass(frozen=True)
class InfrastructureResult:
    """The Terraform outputs and the isolated kube environment after the infrastructure stage."""

    outputs: dict[str, dict[str, object]]
    kube: dict[str, str | None]


def gateway_context(project_id: str, location: str, cluster: str) -> str:
    """Return the kubeconfig context name that Connect Gateway credentials create."""
    return f"connectgateway_{project_id}_{location}_{cluster}"


def apply_broker_overlay(
    runner: ProcessRunner, inputs: DeploymentInputs, *, mode: str, context: str
) -> DeploymentInputs:
    """Bind the reviewed broker overlay to the private config copy and reload the inputs."""
    from installation.loader import load_root_config
    from installation.settings_gcp import GcpBackendSettings

    if inputs.broker_template_path is None:
        return inputs
    overlay = inputs.workdir / "model-broker-overlay.json"
    overlay.unlink(missing_ok=True)
    repo = get_repo_root()
    runner.run(
        script_argv(
            repo,
            "prepare_model_broker_overlay",
            "--template",
            str(inputs.broker_template_path),
            "--project-id",
            inputs.project_id,
            "--context",
            context,
            "--output",
            str(overlay),
            "--mode",
            mode,
        )
    )
    runner.run(
        script_argv(repo, "apply_shifter_config_overlay", "--config", str(inputs.config_path)),
        env={"SHIFTER_CONFIG_OVERLAY_FILE": str(overlay), "SHIFTER_CONFIG_OVERLAY_JSON": None},
    )
    root_config = load_root_config(inputs.config_path)
    # dataclasses.replace returns the type of its argument (DeploymentInputs).
    return dataclasses.replace(  # NOSONAR - S5886 reports the TypeVar bound, not DeploymentInputs.
        inputs, root_config=root_config, settings=GcpBackendSettings.from_root_settings(root_config.settings)
    )


def reconcile_foundation_image_network(runner: ProcessRunner, inputs: DeploymentInputs) -> None:
    """Run the narrow, action-allowlisted foundation image-network reconcile."""
    if inputs.foundation_inputs_path is None:
        raise DeployStageError("the foundation reconcile requires foundation-inputs.json in the deployment inputs")
    runner.run(
        script_argv(get_repo_root(), "reconcile_foundation_image_network"),
        env={
            "GCP_ENVIRONMENT": inputs.environment,
            "GCP_PROJECT_ID": inputs.project_id,
            "GCP_FOUNDATION_INPUTS_JSON": inputs.foundation_inputs_path.read_text(encoding="utf-8"),
            "RUNNER_TEMP": str(inputs.workdir),
        },
    )


def verify_range_telemetry(
    runner: ProcessRunner, inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]
) -> None:
    """Read back the range audit telemetry controls against ``settings.range_telemetry``."""
    network = str(outputs.get("range_network_name", {}).get("value", "")).strip()
    owned = outputs.get("range_telemetry_terraform_resources", {}).get("value")
    if not network or not isinstance(owned, Mapping):
        raise DeployStageError(
            "Terraform outputs range_network_name and range_telemetry_terraform_resources are required"
        )
    range_project = inputs.settings.range_cells.project_id or inputs.project_id  # type: ignore[attr-defined]
    argv = script_argv(
        get_repo_root(),
        "verify_range_audit_telemetry",
        "--config",
        str(inputs.config_path),
        "--project-id",
        inputs.project_id,
        "--range-project-id",
        range_project,
        "--range-network",
        network,
    )
    for name in owned.get("subnetworks") or ():
        argv += ["--terraform-subnetwork", str(name)]
    for name in owned.get("nat_routers") or ():
        argv += ["--terraform-nat-router", str(name)]
    runner.run(argv)


def check_range_preconditions(
    inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]], *, allow_missing: bool
) -> None:
    """Fail before rollout when a range cannot launch, unless platform-first bring-up is chosen."""
    from gcp_control_plane import check_gce_range_preconditions
    from gcp_runtime import gdc_config, renderer_inputs

    if inputs.settings.range_cells.backend != "gce":  # type: ignore[attr-defined]
        return
    check_gce_range_preconditions(
        gdc_config(inputs), allow_missing_range_images=allow_missing, env=renderer_inputs(inputs, outputs)
    )


def seed_operator(inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]], *, password: str) -> None:
    """Create the first Identity Platform operator when operator bootstrap is enabled."""
    from gcp_control_plane import ensure_gcp_identity_platform_operator
    from gcp_runtime import gdc_config

    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    if operator is None or not operator.enabled:
        warn("Operator bootstrap is disabled in settings.operator_bootstrap; no operator is created")
        return
    ensure_gcp_identity_platform_operator(
        gdc_config(inputs), dict(outputs), credentials=(operator.admin_email.strip().lower(), password)
    )


def infrastructure_stage(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    *,
    operator_password: str,
    reconcile_foundation: bool,
) -> tuple[DeploymentInputs, InfrastructureResult]:
    """Prepare the project, apply the platform Terraform, and seed the first operator."""
    from gcp_control_plane import ensure_gcp_artifact_registry_service_identity
    from gcp_prereqs import ensure_identity_platform, ensure_state_bucket, release_disabled_broker_load_balancer
    from gcp_runtime import gdc_config
    from gcp_terraform import (
        artifact_registry_kms_adoption,
        backend_prefix,
        state_bucket_name,
        terraform_apply,
        terraform_context,
        terraform_init,
        terraform_outputs,
    )

    header(f"Infrastructure: {inputs.environment}")
    ensure_identity_platform(runner, {}, project_id=inputs.project_id)
    if reconcile_foundation:
        reconcile_foundation_image_network(runner, inputs)
    cluster = cluster_name(inputs.environment)
    context = gateway_context(inputs.project_id, inputs.region, cluster)
    cluster_exists = (
        runner.run(
            ["gcloud", "container", "fleet", "memberships", "describe", cluster, "--location", inputs.region]
            + ["--project", inputs.project_id],
            check=False,
        ).returncode
        == 0
    )
    early_kube: dict[str, str | None] = {}
    if cluster_exists:
        early_kube = kube_env(
            runner, {}, workdir=inputs.workdir, project_id=inputs.project_id, location=inputs.region, cluster=cluster
        )
        with scoped_environment(early_kube):
            inputs = apply_broker_overlay(runner, inputs, mode="ensure", context=context)
    bucket = state_bucket_name(inputs.project_id)
    ensure_state_bucket(runner, {}, bucket=bucket, project_id=inputs.project_id, region=inputs.region)
    ensure_gcp_artifact_registry_service_identity(gdc_config(inputs))
    ctx = terraform_context(inputs, inputs.workdir, {})
    terraform_init(runner, ctx, state_bucket=bucket, prefix=backend_prefix(inputs.environment))
    if cluster_exists:
        release_disabled_broker_load_balancer(
            runner,
            {},
            broker_enabled=bool(inputs.settings.model_broker.enabled),  # type: ignore[attr-defined]
            project_id=inputs.project_id,
            region=inputs.region,
            cluster=cluster,
            kube_env=early_kube,
        )
    terraform_apply(runner, ctx, extra_args=artifact_registry_kms_adoption(runner, ctx, inputs))
    outputs = terraform_outputs(runner, ctx)
    verify_range_telemetry(runner, inputs, outputs)
    kube = kube_env(
        runner,
        {},
        workdir=inputs.workdir,
        project_id=inputs.project_id,
        location=str(outputs["gke_cluster_location"]["value"]),
        cluster=str(outputs["gke_cluster_name"]["value"]),
    )
    with scoped_environment(kube):
        seed_operator(inputs, outputs, password=operator_password)
    success("Infrastructure stage complete")
    return inputs, InfrastructureResult(outputs=outputs, kube=kube)


def read_infrastructure(runner: ProcessRunner, inputs: DeploymentInputs) -> InfrastructureResult:
    """Read the applied outputs and create the kube environment without changing state."""
    from gcp_terraform import backend_prefix, state_bucket_name, terraform_context, terraform_init, terraform_outputs

    ctx = terraform_context(inputs, inputs.workdir, {})
    terraform_init(
        runner, ctx, state_bucket=state_bucket_name(inputs.project_id), prefix=backend_prefix(inputs.environment)
    )
    outputs = terraform_outputs(runner, ctx)
    kube = kube_env(
        runner,
        {},
        workdir=inputs.workdir,
        project_id=inputs.project_id,
        location=str(outputs["gke_cluster_location"]["value"]),
        cluster=str(outputs["gke_cluster_name"]["value"]),
    )
    return InfrastructureResult(outputs=outputs, kube=kube)


def sync_policy_credentials(
    runner: ProcessRunner, inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]
) -> bool:
    """Synchronize the policy datastore credentials when native policy is enabled."""
    native = outputs.get("native_identity", {}).get("value") or {}
    if not (isinstance(native, Mapping) and native.get("openfga_enabled")):
        return False
    runner.run(script_argv(get_repo_root(), "sync_openfga_secrets", "--config", str(inputs.config_path)))
    return True


def restart_policy_clients(runner: ProcessRunner) -> None:
    """Restart only policy servers; application restore requires native activation."""
    for deployment in ("openfga", "openfga-admin"):
        runner.run(["kubectl", "rollout", "restart", f"deployment/{deployment}", "-n", "shifter-platform"])
    for deployment in ("openfga", "openfga-admin"):
        runner.run(
            ["kubectl", "rollout", "status", f"deployment/{deployment}", "-n", "shifter-platform", "--timeout=10m"]
        )


def retire_inventoried_artifacts(runner: ProcessRunner, inputs: DeploymentInputs, context: str) -> None:
    """Retire manual qualification objects named in the optional inventory."""
    if inputs.retired_artifacts_path is None:
        return
    runner.run(
        script_argv(
            get_repo_root(),
            "reconcile_retired_cluster_artifacts",
            "--context",
            context,
            "--manifest",
            str(inputs.retired_artifacts_path),
        )
    )


@dataclass(frozen=True)
class RolloutRequest:
    """Everything the rollout stage needs besides the inputs and the infrastructure."""

    identities: Mapping[str, str]
    evidence: EvidenceRun
    first_install: bool
    identity_cutover_facts: str = ""
    identity_bootstrap_facts: str = ""
    catalog_credential_file: str = ""


def rollout_stage(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    infrastructure: InfrastructureResult,
    request: RolloutRequest,
) -> bool:
    """Render, migrate, upgrade, verify, and wait. Return True when the certificate is Active."""
    from gcp_catalog import initialize_catalog
    from gcp_control_plane import (
        ensure_gcp_control_plane_namespaces,
        run_gcp_database_migrations,
        sync_gcp_guacamole_runtime_secret,
    )
    from gcp_helm import (
        adopt_objects,
        build_values,
        helm_upgrade,
        profile_values_path,
        remove_retired_objects,
        render_chart,
        write_values,
    )
    from gcp_identity_cutover import (
        activate_identity_cutover,
        identity_cutover_boundary,
        migrate_policy_datastore,
        retire_disabled_broker,
    )
    from gcp_openvpn import ensure_pki, pool_output, roll_out_pool
    from gcp_rollout import (
        record_running_images,
        restart_workloads,
        wait_for_certificate,
        wait_for_ingress_address,
        wait_for_rollouts,
    )
    from gcp_runtime import compose_runtime_env, gdc_config

    header(f"Rollout: {inputs.environment}")
    outputs = infrastructure.outputs
    location = str(outputs["gke_cluster_location"]["value"])
    cluster = str(outputs["gke_cluster_name"]["value"])
    context = gateway_context(inputs.project_id, location, cluster)
    with scoped_environment(infrastructure.kube):
        inputs = apply_broker_overlay(runner, inputs, mode="read", context=context)
        pool = pool_output(outputs)
        pki_changed = (
            ensure_pki(runner, {}, pool=pool, project_id=inputs.project_id, workdir=inputs.workdir) if pool else False
        )
        provisioner = request.identities["provisioner"].rsplit("@", 1)[1]
        runtime_env = compose_runtime_env(
            runner, inputs, outputs, provisioner_digest=provisioner, workdir=inputs.workdir
        )
        values = build_values(inputs, outputs, runtime_env=runtime_env, identities=request.identities)
        broker_enabled = bool((values.get("modelBroker") or {}).get("enabled"))  # type: ignore[union-attr]
        values_file = write_values(inputs.workdir, values)
        values_files = [profile_values_path(inputs.profile), values_file]
        ensure_gcp_control_plane_namespaces()
        documents = render_chart(runner, {}, values_files)
        # Only prerequisite bindings change before a conclusive probe. Policy
        # rotation that makes the incumbent runtime unverifiable stops here.
        policy_enabled = sync_policy_credentials(runner, inputs, outputs)
        with identity_cutover_boundary(
            runner,
            documents,
            inputs.workdir,
            cutover_facts=request.identity_cutover_facts,
            bootstrap_facts=request.identity_bootstrap_facts,
        ) as cutover:
            run_gcp_database_migrations(outputs, values_file)
            if policy_enabled and cutover.required:
                migrate_policy_datastore(runner, documents)
                restart_policy_clients(runner)
            activate_identity_cutover(runner, cutover)
            sync_gcp_guacamole_runtime_secret(gdc_config(inputs), outputs)
            retire_disabled_broker(runner, enabled=broker_enabled)
            adopt_objects(runner, {}, documents)
            helm_upgrade(runner, {}, values_files)
            retire_inventoried_artifacts(runner, inputs, context)
            remove_retired_objects(runner, {})
            if policy_enabled and not cutover.required:
                restart_policy_clients(runner)
        restart_workloads(runner, {}, model_broker_enabled=broker_enabled)
        wait_for_rollouts(runner, {}, model_broker_enabled=broker_enabled)
        if pool:
            roll_out_pool(
                runner,
                {},
                pool=pool,
                image=request.identities["openvpn"],
                project_id=inputs.project_id,
                pki_changed=pki_changed,
            )
        record_running_images(
            runner,
            {},
            identities=request.identities,
            model_broker_enabled=broker_enabled,
            evidence=request.evidence,
            workdir=inputs.workdir,
        )
        wait_for_ingress_address(runner, {})
        active = wait_for_certificate(
            runner, {}, hostname=inputs.public_hostname, require_active=not request.first_install
        )
        initialize_catalog(
            runner,
            documents,
            credential_file=request.catalog_credential_file,
            hostname=inputs.public_hostname,
        )
    success("Rollout stage complete")
    return active


def tool_available(name: str) -> bool:
    """Return True when ``name`` is on PATH."""
    from shutil import which

    return which(name, path=os.environ.get("PATH")) is not None


def require_tools(*names: str) -> None:
    """Fail before any mutation when a required tool is missing."""
    missing = [name for name in names if not tool_available(name)]
    if missing:
        raise DeployStageError("required tools are missing from PATH: " + ", ".join(missing))
    info("Required tools present: " + ", ".join(names))
