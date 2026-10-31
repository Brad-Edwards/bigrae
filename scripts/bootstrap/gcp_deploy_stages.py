"""Stages of the GitHub-less GCP deploy command (#2571, ADR-070).

Each stage takes validated inputs and runs under one purpose identity that the caller
selects with :func:`gcp_process.scoped_environment`. The stages are the single deploy
implementation: the operator command runs them in order, and CI runs the same stages in
separate jobs.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bootstrap_core import get_repo_root, header, info, success, warn
from gcp_evidence import EvidenceRun
from gcp_inputs import DeploymentInputs, private_workdir, write_private_file
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


def operator_bootstrap_facts(inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]) -> Path:
    """Write the bootstrap facts that name the configured operator account (#2679).

    The infrastructure stage created that Identity Platform account from
    ``settings.operator_bootstrap.admin_email``. The facts carry only the issuer
    and the subject, in an owner-only file in the private work directory.
    """
    from gcp_control_plane import gcp_identity_platform_subject
    from gcp_runtime import gdc_config

    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    if operator is None or not operator.enabled:
        raise DeployStageError("A fresh install needs settings.operator_bootstrap to create the first administrator")
    project = str(outputs["identity_platform_project_id"]["value"])
    subject = gcp_identity_platform_subject(gdc_config(inputs), dict(outputs), operator.admin_email)
    facts = {
        "schema": "shifter.identity-bootstrap-facts/v1",
        "issuer": f"https://securetoken.google.com/{project}",
        "subject": subject,
    }
    directory = private_workdir(inputs.workdir / "identity")
    return write_private_file(directory / "bootstrap.json", json.dumps(facts, sort_keys=True))


def fresh_install_facts(inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]) -> tuple[Path, Path]:
    """Write the cutover and bootstrap facts of an empty installation (#2679).

    An empty database has no organizations, workspaces, or users to map. The
    first administrator may issue personal credentials, which the catalog
    registration and the first sign-in use.
    """
    bootstrap = operator_bootstrap_facts(inputs, outputs)
    cutover = {
        "schema": "shifter.identity-cutover-facts/v1",
        "account_types_by_organization": {},
        # A boolean eligibility flag of the facts schema, not a credential value.
        "bootstrap_personal_token_eligibility": True,  # nosec B105
        "owner_user_ids_by_workspace": {},
    }
    return write_private_file(bootstrap.parent / "cutover.json", json.dumps(cutover, sort_keys=True)), bootstrap


def infrastructure_stage(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    *,
    operator_password: str,
    reconcile_foundation: bool,
) -> tuple[DeploymentInputs, InfrastructureResult]:
    """Prepare the project, apply the platform Terraform, and seed the first operator."""
    from gcp_control_plane import ensure_gcp_artifact_registry_service_identity
    from gcp_prereqs import (
        ensure_identity_platform,
        ensure_state_bucket,
        release_disabled_broker_load_balancer,
        remove_deleted_role_bindings,
    )
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
    remove_deleted_role_bindings(runner, {}, project_id=inputs.project_id)
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
    adopt_kms = artifact_registry_kms_adoption(runner, ctx, inputs)
    terraform_apply(runner, ctx, variables={"adopt_artifact_registry_kms": "true"} if adopt_kms else None)
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


def policy_settings(inputs: DeploymentInputs) -> object:
    """Return the OpenFGA settings with the derived secret references filled in (#2679)."""
    from installation.settings_gcp import resolve_openfga_settings

    return resolve_openfga_settings(
        inputs.settings.openfga,  # type: ignore[attr-defined]
        project_id=inputs.project_id,
        deployment=inputs.environment,
    )


def sync_policy_credentials(
    runner: ProcessRunner, inputs: DeploymentInputs, outputs: Mapping[str, Mapping[str, object]]
) -> bool:
    """Create the OpenFGA credentials on first use, then synchronize them into the cluster."""
    from gcp_policy import ensure_policy_secrets

    native = outputs.get("native_identity", {}).get("value") or {}
    if not (isinstance(native, Mapping) and native.get("openfga_enabled")):
        return False
    policy = policy_settings(inputs)
    ensure_policy_secrets(
        runner,
        {
            "runtime": policy.runtime_secret_ref,  # type: ignore[attr-defined]
            "admin": policy.admin_secret_ref,  # type: ignore[attr-defined]
            "ca": policy.ca_secret_ref,  # type: ignore[attr-defined]
        },
    )
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
    # The release's OpenFGA model, which each deploy publishes unless the settings pin one (#2679).
    authorization_model: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RenderedRelease:
    """One local render of the Helm release and the values that produced it."""

    values: dict[str, object]
    values_file: Path
    values_files: list[Path]
    documents: list[dict[str, object]]


def render_release(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Mapping[str, object]],
    *,
    runtime_env: Mapping[str, str],
    identities: Mapping[str, str],
    binding: tuple[str, str] | None,
) -> RenderedRelease:
    """Write the generated values and render the chart with the given policy binding."""
    from gcp_helm import build_values, profile_values_path, render_chart, write_values

    values = build_values(inputs, outputs, runtime_env=runtime_env, identities=identities, policy_binding=binding)
    values_file = write_values(inputs.workdir, values)
    values_files = [profile_values_path(inputs.profile), values_file]
    return RenderedRelease(values, values_file, values_files, render_chart(runner, {}, values_files))


def _finish_rollout(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    request: RolloutRequest,
    *,
    pool: object,
    pki_changed: bool,
    broker_enabled: bool,
    documents: list[dict[str, object]],
) -> None:
    """Restart and wait for the workloads, roll the VPN pool, and record the running images."""
    from gcp_openvpn import roll_out_pool
    from gcp_rollout import ReleasePins, record_running_images, restart_workloads, wait_for_rollouts

    pins = ReleasePins.from_documents(documents)
    restart_workloads(runner, {}, model_broker_enabled=broker_enabled)
    wait_for_rollouts(runner, {}, model_broker_enabled=broker_enabled, policy_enabled=pins.policy is not None)
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
        pins=pins,
    )


def _publish_before_probe(
    runner: ProcessRunner,
    request: RolloutRequest,
    render: Callable[..., RenderedRelease],
    release: RenderedRelease,
    *,
    binding: tuple[str, str] | None,
    policy_enabled: bool,
) -> tuple[tuple[str, str] | None, RenderedRelease]:
    """Publish first on an existing installation and render with the published IDs (#2694).

    The identity probe checks an activated cutover against the rendered
    binding. A fresh install has no policy servers yet; it publishes later.
    """
    from gcp_policy import policy_admin_available, publish_policy_model, release_model

    if not policy_enabled or binding is not None or not policy_admin_available(runner):
        return binding, release
    binding = publish_policy_model(runner, release.documents, release_model(request.authorization_model))
    return binding, render(binding=binding)


def rollout_stage(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    infrastructure: InfrastructureResult,
    request: RolloutRequest,
) -> bool:
    """Render, migrate, upgrade, verify, and wait. Return True when the certificate is Active."""
    from gcp_control_plane import (
        ensure_gcp_control_plane_namespaces,
        run_gcp_database_migrations,
        sync_gcp_guacamole_runtime_secret,
    )
    from gcp_helm import adopt_objects, helm_upgrade, remove_retired_objects
    from gcp_identity_cutover import (
        activate_identity_cutover,
        identity_cutover_boundary,
        migrate_policy_datastore,
        retire_disabled_broker,
    )
    from gcp_openvpn import ensure_pki, pool_output
    from gcp_policy import publish_policy_model, release_model
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
        render = functools.partial(
            render_release, runner, inputs, outputs, runtime_env=runtime_env, identities=request.identities
        )
        # Pinned settings IDs bind the policy client now; otherwise each deploy
        # publishes the release model after the policy servers are ready (#2679).
        binding = policy_settings(inputs).pinned_binding  # type: ignore[attr-defined]
        release = render(binding=binding)
        broker_enabled = bool((release.values.get("modelBroker") or {}).get("enabled"))  # type: ignore[union-attr]
        ensure_gcp_control_plane_namespaces()
        # Only prerequisite bindings change before a conclusive probe. Policy
        # rotation that makes the incumbent runtime unverifiable stops here.
        policy_enabled = sync_policy_credentials(runner, inputs, outputs)
        binding, release = _publish_before_probe(
            runner, request, render, release, binding=binding, policy_enabled=policy_enabled
        )
        # The probe connects as a native login, which needs its SQL duty first (#2704).
        run_gcp_database_migrations(outputs, release.values_file, duties_only=True)
        with identity_cutover_boundary(
            runner,
            release.documents,
            inputs.workdir,
            cutover_facts=request.identity_cutover_facts,
            bootstrap_facts=request.identity_bootstrap_facts,
            fresh_install_facts=lambda: fresh_install_facts(inputs, outputs),
        ) as cutover:
            run_gcp_database_migrations(outputs, release.values_file)
            if policy_enabled and cutover.required:
                migrate_policy_datastore(runner, release.documents)
                restart_policy_clients(runner)
            if policy_enabled and binding is None:
                binding = publish_policy_model(runner, release.documents, release_model(request.authorization_model))
                release = render(binding=binding)
                cutover = cutover.with_documents(release.documents)
            activate_identity_cutover(runner, cutover)
            sync_gcp_guacamole_runtime_secret(gdc_config(inputs), outputs)
            retire_disabled_broker(runner, enabled=broker_enabled)
            adopt_objects(runner, {}, release.documents)
            helm_upgrade(runner, {}, release.values_files)
            retire_inventoried_artifacts(runner, inputs, context)
            remove_retired_objects(runner, {})
            if policy_enabled and not cutover.required:
                restart_policy_clients(runner)
        _finish_rollout(
            runner,
            inputs,
            request,
            pool=pool,
            pki_changed=pki_changed,
            broker_enabled=broker_enabled,
            documents=release.documents,
        )
        active = _open_access(runner, inputs, outputs, request, documents=release.documents, cutover=cutover)
    success("Rollout stage complete")
    return active


def _open_access(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    outputs: Mapping[str, Mapping[str, object]],
    request: RolloutRequest,
    *,
    documents: list[dict[str, object]],
    cutover: object,
) -> bool:
    """Wait for the ingress and the certificate, then register the catalog.

    Return True when the certificate is Active.
    """
    from gcp_catalog import initialize_catalog
    from gcp_rollout import wait_for_certificate, wait_for_ingress_address

    wait_for_ingress_address(runner, {})
    active = wait_for_certificate(runner, {}, hostname=inputs.public_hostname, require_active=not request.first_install)
    initialize_catalog(
        runner,
        documents,
        credential_file=request.catalog_credential_file,
        bootstrap_facts=_catalog_bootstrap_facts(inputs, outputs, cutover, request.catalog_credential_file),
        hostname=inputs.public_hostname,
    )
    return active


def _catalog_bootstrap_facts(
    inputs: DeploymentInputs,
    outputs: Mapping[str, Mapping[str, object]],
    cutover: object,
    credential_file: str,
) -> Path | None:
    """Return the bootstrap facts that register the in-box catalog, or None to skip it (#2679).

    A supplied credential takes precedence. Otherwise the catalog Job acts as the
    configured operator, which must be the recorded bootstrap administrator.
    """
    if credential_file:
        return None
    if getattr(cutover, "bootstrap_facts", None) is not None:
        return cutover.bootstrap_facts  # type: ignore[attr-defined]
    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    if operator is None or not operator.enabled:
        return None
    try:
        return operator_bootstrap_facts(inputs, outputs)
    except RuntimeError as exc:
        # The application is active; a lookup failure leaves only catalog work pending.
        warn(f"Cannot name the bootstrap administrator for catalog registration ({type(exc).__name__})")
        return None


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
