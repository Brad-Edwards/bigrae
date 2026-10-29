"""GitHub-less GCP platform teardown (#2571, ADR-070).

``gcp-destroy`` tears down one deployment's platform-core Terraform state as the
destroy purpose identity. It keeps the foundation identities, the Terraform state
bucket, the release-evidence bucket, and the deployment config bucket.

Safety controls:

- an explicit ``--confirm <environment>`` that must equal the selected deployment;
- the deployment lock, so a deploy and a destroy never overlap;
- in operator mode, tooling whose revision equals a release whose images verify against
  the deployment trust anchor (this replaces the protected-branch rule of the workflow);
- the same private Terraform inputs as the deploy, with Cloud SQL deletion protection
  lifted and the permanent KMS key ring and key removed from state first.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bootstrap_core import header, info, success
from gcp_deploy import (
    DeployOptions,
    GcpDeployError,
    acquire_deploy_lock,
    check_tooling_revision,
    input_fetch_env,
    load_manifest,
    new_run_id,
    release_deploy_lock,
    release_trust,
)
from gcp_deploy_stages import require_tools
from gcp_identity import PurposeIdentities
from gcp_inputs import DeploymentInputs, load_deployment_inputs
from gcp_lock import DeployLock
from gcp_process import ProcessRunner, scoped_environment


@dataclass(frozen=True)
class DestroyOptions:
    """Parsed options of ``gcp-destroy``."""

    source: str
    confirm: str
    environment: str = ""
    release_manifest: str = ""
    identity_mode: str = "impersonate"
    name_prefix: str = ""
    destroy_service_account: str = ""
    lock: str = "gcs"
    break_lock: bool = False
    trusted_repository_id: str = ""
    trusted_owner_id: str = ""


def _verify_release_tooling(runner: ProcessRunner, options: DestroyOptions, inputs: DeploymentInputs) -> None:
    """Verify the release images and the tooling revision before teardown (operator mode)."""
    from gcp_prereqs import access_token
    from gcp_provenance import verify_release_provenance

    manifest = load_manifest(options.release_manifest)
    check_tooling_revision(manifest.source_revision)
    trust = release_trust(
        inputs,
        DeployOptions(
            source=options.source,
            trusted_repository_id=options.trusted_repository_id,
            trusted_owner_id=options.trusted_owner_id,
        ),
        manifest.source_revision,
    )
    verify_release_provenance(dict(manifest.images), trust, token_provider=lambda: access_token(runner, {}))


def remove_platform_release(runner: ProcessRunner, inputs: DeploymentInputs) -> None:
    """Uninstall the platform Helm release before Terraform removes the cluster.

    The GKE Ingress controller owns the load balancer, its backend services, and
    its network endpoint groups. A cluster delete leaves them behind, and the
    backend services keep the Cloud Armor policy in use, so Terraform can not
    delete it. Uninstalling the release first lets the controller remove them
    (#2640). The step is a no-op when the cluster or the release is gone.
    """
    from gcp_helm import RELEASE_NAME, RELEASE_NAMESPACE
    from gcp_kube import cluster_name, kube_env

    cluster = cluster_name(inputs.environment)
    membership = runner.run(
        ["gcloud", "container", "fleet", "memberships", "describe", cluster]
        + ["--location", inputs.region, "--project", inputs.project_id],
        check=False,
    )
    if membership.returncode != 0:
        info(f"Cluster {cluster} is not registered; skipping the Helm uninstall")
        return
    kube = kube_env(
        runner, {}, workdir=inputs.workdir, project_id=inputs.project_id, location=inputs.region, cluster=cluster
    )
    if runner.run(["helm", "status", RELEASE_NAME, "--namespace", RELEASE_NAMESPACE], env=kube, check=False).returncode:
        info(f"Helm release {RELEASE_NAME} is not installed; skipping the uninstall")
        return
    info(f"Uninstalling Helm release {RELEASE_NAME} so the Ingress controller removes its load balancer")
    runner.run(
        ["helm", "uninstall", RELEASE_NAME, "--namespace", RELEASE_NAMESPACE, "--wait", "--timeout", "15m"],
        env=kube,
        capture=False,
    )


def run_destroy(options: DestroyOptions, *, runner: ProcessRunner | None = None) -> int:
    """Run ``gcp-destroy``."""
    from gcp_terraform import (
        apply_destroy_settings,
        backend_prefix,
        drop_unremovable_kms_resources,
        state_bucket_name,
        terraform_context,
        terraform_destroy,
        terraform_init,
    )

    runner = runner or ProcessRunner()
    require_tools("gcloud", "gke-gcloud-auth-plugin", "terraform", "helm")
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-destroy-"))
    lock: DeployLock | None = None
    destroy_env: dict[str, str | None] = {}
    lock_options = DeployOptions(source=options.source, lock=options.lock, break_lock=options.break_lock)
    try:
        inputs = load_deployment_inputs(
            options.source,
            workdir,
            runner,
            input_fetch_env(DeployOptions(source=options.source, identity_mode=options.identity_mode)),
            environment=options.environment,
        )
        if options.confirm != inputs.environment:
            raise GcpDeployError(f"--confirm must equal the deployment name {inputs.environment!r} to destroy it")
        identities = PurposeIdentities.resolve(
            mode=options.identity_mode,  # type: ignore[arg-type]
            project_id=inputs.project_id,
            environment=inputs.environment,
            name_prefix=options.name_prefix,
            destroy=options.destroy_service_account,
        )
        destroy_env = identities.env("destroy")
        header(f"GCP destroy: {inputs.environment}")
        with scoped_environment(destroy_env):
            if options.identity_mode == "impersonate":
                _verify_release_tooling(runner, options, inputs)
        lock = acquire_deploy_lock(runner, lock_options, inputs, destroy_env, "gcp-destroy", new_run_id())
        with scoped_environment(destroy_env):
            ctx = terraform_context(inputs, workdir, {}, destroy=True)
            terraform_init(
                runner,
                ctx,
                state_bucket=state_bucket_name(inputs.project_id),
                prefix=backend_prefix(inputs.environment),
            )
            remove_platform_release(runner, inputs)
            apply_destroy_settings(runner, ctx)
            drop_unremovable_kms_resources(runner, ctx)
            terraform_destroy(runner, ctx)
        success(f"GCP deployment {inputs.environment} platform resources are destroyed")
        return 0
    finally:
        try:
            release_deploy_lock(runner, lock, destroy_env)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
