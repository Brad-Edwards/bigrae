"""GitHub-less GCP deploy, release-scan, smoke, and destroy commands (#2571, ADR-070).

``gcp-deploy`` stands up or updates one GCP deployment from a published release:

1. load and validate the deployment input bundle (config bucket or directory);
2. check that the tooling revision equals the release revision (operator mode);
3. take the deployment lock;
4. run the infrastructure stage as the deploy identity;
5. import the release images into the deployment registry;
6. scan the exact digests as the release-scan identity;
7. run the rollout stage as the deploy identity;
8. run the advisory post-deploy smoke.

CI runs the same stages in separate jobs (``--stage``) with ``--identity ambient``.
``gcp-destroy`` tears the platform down as the destroy identity.
"""

from __future__ import annotations

import dataclasses
import os
import secrets
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bootstrap_core import error, get_repo_root, header, info, success, warn
from gcp_deploy_stages import (
    InfrastructureResult,
    RolloutRequest,
    infrastructure_stage,
    read_infrastructure,
    require_tools,
    rollout_stage,
)
from gcp_evidence import EvidenceRun
from gcp_identity import PurposeIdentities
from gcp_inputs import DeploymentInputs, is_gcs_url, load_deployment_inputs, resolve_operator_password
from gcp_lock import DeployLock, acquire_lock, break_lock, release_lock
from gcp_process import ProcessRunner, scoped_environment
from gcp_smoke import SMOKE_FAILED_EXIT_STATUS, run_smoke

if TYPE_CHECKING:
    from installation.gcp_release import GcpReleaseManifest

    from gcp_provenance import ReleaseTrust

STAGES = ("all", "infrastructure", "rollout")
DEFAULT_RELEASE_MANIFEST_ENV = "BIGRAE_RELEASE_MANIFEST"
SOURCE_REVISION_ENV = "BIGRAE_SOURCE_REVISION"


class GcpDeployError(RuntimeError):
    """A GCP lifecycle command cannot continue."""


@dataclass(frozen=True)
class DeployOptions:
    """Parsed options of ``gcp-deploy``."""

    source: str
    environment: str = ""
    release_manifest: str = ""
    release_source: str = ""
    stage: str = "all"
    identity_mode: str = "impersonate"
    name_prefix: str = ""
    deploy_service_account: str = ""
    release_scan_service_account: str = ""
    lock: str = "gcs"
    break_lock: bool = False
    first_install: bool = False
    identity_cutover_facts: str = ""
    identity_bootstrap_facts: str = ""
    catalog_credential_file: str = ""
    allow_missing_range_images: bool = False
    reconcile_foundation_image_network: bool = False
    smoke: bool = True
    operator_password_env: str = ""
    trusted_repository_id: str = ""
    trusted_owner_id: str = ""
    run_id: str = ""
    attempt: int = 1
    evidence_bucket: str = ""
    image_roots_output: str = ""


def operator_mode(identity_mode: str) -> bool:
    """True when the caller impersonates the purpose identities (an operator, not CI)."""
    return identity_mode == "impersonate"


def scans_release(stage: str, identity_mode: str) -> bool:
    """True when this run must scan the exact release digests before the rollout.

    CI scans in its separate release-scan job before the rollout job starts. An
    operator rollout always scans, so no operator stage can skip the gate.
    """
    return stage == "all" or (stage == "rollout" and operator_mode(identity_mode))


def required_tools(stage: str, identity_mode: str) -> tuple[str, ...]:
    """Return the tools that one ``gcp-deploy`` run uses.

    kubectl reaches the private cluster through Connect Gateway with
    gke-gcloud-auth-plugin. A scanning run also imports the release images.
    """
    tools = ["gcloud", "gke-gcloud-auth-plugin", "terraform", "kubectl"]
    if stage != "infrastructure":
        tools.append("helm")
    if scans_release(stage, identity_mode):
        tools += ["oras", "trivy"]
    return tuple(tools)


def new_run_id(now: datetime | None = None) -> str:
    """Return a lowercase, DNS-label-safe run id."""
    moment = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    return f"{moment}-{secrets.token_hex(4)}"


def tooling_revision(repo_root: Path) -> tuple[str, bool]:
    """Return the tooling source revision and whether the tooling is unmodified."""
    embedded = os.environ.get(SOURCE_REVISION_ENV, "").strip()
    if embedded:
        return embedded, True
    git = shutil.which("git")
    if git is None:
        return "", False
    # Fixed git argv from shutil.which; no shell and no operator input.
    head = subprocess.run(  # nosec B603
        [git, "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    status = subprocess.run(  # nosec B603
        [git, "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=False,
    )
    clean = head.returncode == 0 and status.returncode == 0 and not status.stdout.strip()
    return head.stdout.strip(), clean


def check_tooling_revision(source_revision: str, *, repo_root: Path | None = None) -> None:
    """Require tooling that equals the verified release revision with no local changes."""
    revision, clean = tooling_revision(repo_root or get_repo_root())
    if revision != source_revision:
        raise GcpDeployError(
            "the deploy tooling revision does not match the release revision; run the release's deployer image "
            "or a checkout of the release revision"
        )
    if not clean:
        raise GcpDeployError("the deploy tooling has local changes; run the release tooling unmodified")
    info("Deploy tooling matches the release revision")


def load_manifest(path: str) -> GcpReleaseManifest:
    """Load the release manifest from ``path`` or the deployer image default."""
    from installation.errors import InstallationConfigError
    from installation.gcp_release import load_release_manifest

    selected = path or os.environ.get(DEFAULT_RELEASE_MANIFEST_ENV, "")
    if not selected:
        raise GcpDeployError("a release manifest is required; pass --release-manifest or use the deployer image")
    try:
        return load_release_manifest(Path(selected))
    except InstallationConfigError as exc:
        raise GcpDeployError(str(exc)) from None


def release_trust(inputs: DeploymentInputs, options: DeployOptions, source_revision: str) -> ReleaseTrust:
    """Resolve the provenance trust anchor from config, or from the CI runtime context."""
    from gcp_provenance import ReleaseTrust

    configured = inputs.settings.release_trust  # type: ignore[attr-defined]
    repository_id = configured.github_repository_id if configured else ""
    owner_id = configured.github_owner_id if configured else ""
    if options.trusted_repository_id or options.trusted_owner_id:
        if configured and (options.trusted_repository_id, options.trusted_owner_id) != (repository_id, owner_id):
            raise GcpDeployError("the trusted repository flags conflict with settings.release_trust")
        repository_id, owner_id = options.trusted_repository_id, options.trusted_owner_id
    if not repository_id or not owner_id:
        raise GcpDeployError("settings.release_trust is required to verify release provenance")
    return ReleaseTrust(repository_id=repository_id, owner_id=owner_id, source_revision=source_revision)


def evidence_bucket(options: DeployOptions, project_id: str) -> str:
    """Return the private release-evidence bucket."""
    return options.evidence_bucket or f"{project_id}-release-evidence"


def _identities(options: DeployOptions, inputs: DeploymentInputs, destroy: str = "") -> PurposeIdentities:
    """Resolve the purpose identities of one run from its options."""
    return PurposeIdentities.resolve(
        mode=options.identity_mode,  # type: ignore[arg-type]
        project_id=inputs.project_id,
        environment=inputs.environment,
        name_prefix=options.name_prefix,
        deploy=options.deploy_service_account,
        release_scan=options.release_scan_service_account,
        destroy=destroy,
    )


def input_fetch_env(options: DeployOptions) -> dict[str, str | None]:
    """Read the inputs as the explicit deploy identity, or as the caller."""
    if options.identity_mode == "impersonate" and options.deploy_service_account:
        return dict.fromkeys(
            (
                "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
                "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
                "GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT",
            ),
            options.deploy_service_account,
        )
    return {}


def acquire_deploy_lock(
    runner: ProcessRunner,
    options: DeployOptions,
    inputs: DeploymentInputs,
    env: Mapping[str, str | None],
    command: str,
    run_id: str,
) -> DeployLock | None:
    """Take the deployment lock in the config bucket, or return None for an external lock."""
    if options.lock == "external":
        info("Deployment lock: external (the caller serializes deploys)")
        return None
    if not is_gcs_url(options.source):
        raise GcpDeployError("the GCS deployment lock requires --store gs://...; use --lock external otherwise")
    with scoped_environment(env):
        if options.break_lock and break_lock(runner, options.source, {}):
            warn("Removed a stale deployment lock")
        return acquire_lock(runner, options.source, {}, command=command, run_id=run_id, workdir=inputs.workdir)


def release_deploy_lock(runner: ProcessRunner, lock: DeployLock | None, env: Mapping[str, str | None]) -> None:
    """Release a lock that this run holds.

    Callers run this in a ``finally`` block. When the run already failed, a
    release failure must not replace that error (#2676): warn that the lock
    stays, and let the original error propagate.
    """
    if lock is None:
        return
    failed_run = sys.exc_info()[1]
    try:
        with scoped_environment(env):
            release_lock(runner, lock, {})
    except Exception as exc:
        if failed_run is None:
            raise
        warn(f"The deployment lock was not released ({exc}). Run the next command with --break-lock.")


def source_images(manifest: GcpReleaseManifest, release_source: str) -> dict[str, str]:
    """Return the release image identities in the release registry or in the operator's mirror."""
    from gcp_release_images import release_identities

    return release_identities(manifest.images, release_source)


def _import_images(
    runner: ProcessRunner,
    inputs: DeploymentInputs,
    infrastructure: InfrastructureResult,
    manifest: GcpReleaseManifest,
    release_source: str,
) -> dict[str, str]:
    """Import the release images into the deployment registry and return their identities."""
    from gcp_release_images import deployment_image_roots, import_release_images

    return import_release_images(
        runner,
        {},
        release_images=source_images(manifest, release_source),
        roots=deployment_image_roots(infrastructure.outputs),
        source_revision=manifest.source_revision,
        workdir=inputs.workdir,
    )


def _scan(runner: ProcessRunner, identities: Mapping[str, str], evidence: EvidenceRun, workdir: Path) -> None:
    """Scan the exact release digests and store the evidence; a finding stops the run."""
    from gcp_release_scan import run_release_scan

    run_release_scan(
        runner,
        {},
        identities=identities,
        evidence=evidence,
        workdir=workdir,
        ignore_file=get_repo_root() / ".trivyignore.yaml",
    )


def _verify(runner: ProcessRunner, identities: Mapping[str, str], trust: ReleaseTrust) -> None:
    """Verify the provenance of every image identity against the trust anchor."""
    from gcp_prereqs import access_token
    from gcp_provenance import verify_release_provenance

    verify_release_provenance(identities, trust, token_provider=lambda: access_token(runner, {}))


def import_release_base_images(
    runner: ProcessRunner,
    options: DeployOptions,
    inputs: DeploymentInputs,
    manifest: GcpReleaseManifest | None,
    trust: ReleaseTrust | None,
) -> DeploymentInputs:
    """Import the release's guest images when the bundle has none yet (#2679, #2668).

    Every guest is verified before any change: its artifact contract and a Sigstore
    bundle that satisfies the trust anchor at its build revision, from a protected build
    branch. The import runs as the operator under the deployment lock: the deploy
    identity holds no image administration (ADR-004-R23). An unchanged digest reuses the
    existing image, and the references go to ``range-images.json`` in the store.
    """
    if (
        manifest is None
        or trust is None
        or not manifest.base_images
        or inputs.settings.range_cells.backend != "gce"  # type: ignore[attr-defined]
        or {"linux", "kali", "dc"} <= set(inputs.range_images)
    ):
        return inputs
    from gcp_base_images import import_selection, range_images_from_imports, render_range_image_env, resolve_selection
    from gcp_oci import OciClient
    from gcp_prereqs import access_token
    from gcp_provenance import default_verifier

    artifacts = resolve_selection(
        dict(manifest.base_images),
        repository_id=trust.repository_id,
        owner_id=trust.owner_id,
        oci=OciClient(lambda: access_token(runner, {})),
        verifier=default_verifier(),
        release_source=options.release_source,
    )
    imported = import_selection(
        artifacts,
        project=inputs.project_id,
        region=str(inputs.settings.region),  # type: ignore[attr-defined]
    )
    images = range_images_from_imports(render_range_image_env(imported))
    if options.source.startswith("gs://"):
        from gcp_config_store import write_range_images

        write_range_images(options.source, images)
    return cast(DeploymentInputs, dataclasses.replace(inputs, range_images={**inputs.range_images, **images}))


def _release(options: DeployOptions, inputs: DeploymentInputs) -> tuple[GcpReleaseManifest | None, ReleaseTrust | None]:
    """Load the release manifest and its trust anchor when the run needs them.

    CI builds its images after the infrastructure stage, so only a CI
    infrastructure run has no release manifest yet.
    """
    if options.stage == "infrastructure" and not operator_mode(options.identity_mode):
        return None, None
    manifest = load_manifest(options.release_manifest)
    return manifest, release_trust(inputs, options, manifest.source_revision)


def _required_release(
    manifest: GcpReleaseManifest | None, trust: ReleaseTrust | None
) -> tuple[GcpReleaseManifest, ReleaseTrust]:
    """Return the release manifest and its trust anchor, or stop when the run has none."""
    if manifest is None or trust is None:
        raise GcpDeployError("this stage requires the release manifest and its trust anchor")
    return manifest, trust


def _operator_gates(
    runner: ProcessRunner,
    manifest: GcpReleaseManifest,
    trust: ReleaseTrust,
    env: Mapping[str, str | None],
    release_source: str,
) -> None:
    """Prove that the tooling is the release revision and that the release images verify (ADR-070-R6)."""
    check_tooling_revision(manifest.source_revision)
    with scoped_environment(env):
        _verify(runner, source_images(manifest, release_source), trust)


def _infrastructure(
    runner: ProcessRunner, options: DeployOptions, inputs: DeploymentInputs
) -> tuple[DeploymentInputs, InfrastructureResult]:
    """Run the infrastructure stage, or read its outputs for a rollout-only run."""
    if options.stage in ("all", "infrastructure"):
        # Only the infrastructure stage seeds the first operator.
        password = resolve_operator_password(inputs, runner, {}, password_env=options.operator_password_env)
        inputs, infrastructure = infrastructure_stage(
            runner,
            inputs,
            operator_password=password,
            reconcile_foundation=options.reconcile_foundation_image_network,
        )
    else:
        infrastructure = read_infrastructure(runner, inputs)
    if options.image_roots_output:
        from gcp_release_images import write_image_roots

        write_image_roots(Path(options.image_roots_output), infrastructure.outputs)
    return inputs, infrastructure


def _smoke(
    runner: ProcessRunner,
    options: DeployOptions,
    inputs: DeploymentInputs,
    infrastructure: InfrastructureResult,
    run_id: str,
) -> bool:
    """Run the advisory smoke after a full deploy. Return True when it passed or did not run."""
    if options.stage != "all" or not options.smoke:
        return True
    with scoped_environment(infrastructure.kube):
        return run_smoke(
            runner,
            {},
            user_email=inputs.settings.post_deploy_smoke.user_email,  # type: ignore[attr-defined]
            run_id=run_id,
        )


def run_deploy(options: DeployOptions, *, runner: ProcessRunner | None = None) -> int:
    """Run ``gcp-deploy``. Return 0, or 3 when only the advisory smoke failed."""
    if options.stage not in STAGES:
        raise GcpDeployError(f"--stage must be one of {', '.join(STAGES)}")
    from gcp_catalog import validate_catalog_credential_path
    from gcp_identity_cutover import validate_fact_paths

    validate_fact_paths(options.identity_cutover_facts, options.identity_bootstrap_facts)
    validate_catalog_credential_path(options.catalog_credential_file)
    runner = runner or ProcessRunner()
    require_tools(*required_tools(options.stage, options.identity_mode))
    run_id = options.run_id or new_run_id()
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-deploy-"))
    lock: DeployLock | None = None
    deploy_env: dict[str, str | None] = {}
    try:
        inputs = load_deployment_inputs(
            options.source,
            workdir,
            runner,
            input_fetch_env(options),
            environment=options.environment,
        )
        identities = _identities(options, inputs)
        deploy_env = identities.env("deploy")
        manifest, trust = _release(options, inputs)
        header(f"GCP deploy: {inputs.environment} ({options.stage})")
        if operator_mode(options.identity_mode):
            # Before any change, every operator stage proves that its tooling is
            # the release revision and that the release images verify against the
            # deployment trust anchor (ADR-070-R6).
            _operator_gates(runner, *_required_release(manifest, trust), deploy_env, options.release_source)
        lock = acquire_deploy_lock(runner, options, inputs, deploy_env, "gcp-deploy", run_id)
        if operator_mode(options.identity_mode):
            inputs = import_release_base_images(runner, options, inputs, manifest, trust)
        with scoped_environment(deploy_env):
            inputs, infrastructure = _infrastructure(runner, options, inputs)
            if options.stage == "infrastructure":
                return 0
            manifest, trust = _required_release(manifest, trust)
            from gcp_deploy_stages import check_range_preconditions

            check_range_preconditions(inputs, infrastructure.outputs, allow_missing=options.allow_missing_range_images)
            images = _import_images(runner, inputs, infrastructure, manifest, options.release_source)
        evidence = EvidenceRun(
            bucket=evidence_bucket(options, inputs.project_id),
            source_revision=manifest.source_revision,
            run_id=run_id,
            attempt=options.attempt,
        )
        if scans_release(options.stage, options.identity_mode):
            with scoped_environment(identities.env("release_scan")):
                _scan(runner, images, evidence, workdir)
        with scoped_environment(deploy_env):
            _verify(runner, images, trust)
            request = RolloutRequest(
                identities=images,
                evidence=evidence,
                first_install=options.first_install,
                identity_cutover_facts=options.identity_cutover_facts,
                identity_bootstrap_facts=options.identity_bootstrap_facts,
                catalog_credential_file=options.catalog_credential_file,
                authorization_model=manifest.authorization_model,
            )
            rollout_stage(runner, inputs, infrastructure, request)
            smoke_ok = _smoke(runner, options, inputs, infrastructure, run_id)
        success(f"GCP deployment {inputs.environment} is current at release {manifest.version}")
        return 0 if smoke_ok else SMOKE_FAILED_EXIT_STATUS
    finally:
        try:
            release_deploy_lock(runner, lock, deploy_env)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


@dataclass(frozen=True)
class ScanOptions:
    """Parsed options of ``gcp-release-scan``."""

    release_manifest: str
    project_id: str
    environment: str
    identity_mode: str = "impersonate"
    name_prefix: str = ""
    release_scan_service_account: str = ""
    evidence_bucket: str = ""
    run_id: str = ""
    attempt: int = 1
    release_source: str = ""


def run_release_scan_command(options: ScanOptions, *, runner: ProcessRunner | None = None) -> int:
    """Run ``gcp-release-scan`` for the images of a release manifest."""
    runner = runner or ProcessRunner()
    require_tools("gcloud", "trivy")
    manifest = load_manifest(options.release_manifest)
    identities = PurposeIdentities.resolve(
        mode=options.identity_mode,  # type: ignore[arg-type]
        project_id=options.project_id,
        environment=options.environment,
        name_prefix=options.name_prefix,
        release_scan=options.release_scan_service_account,
    )
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-scan-"))
    try:
        with scoped_environment(identities.env("release_scan")):
            evidence = EvidenceRun(
                bucket=options.evidence_bucket or f"{options.project_id}-release-evidence",
                source_revision=manifest.source_revision,
                run_id=options.run_id or new_run_id(),
                attempt=options.attempt,
            )
            _scan(runner, source_images(manifest, options.release_source), evidence, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


def run_smoke_command(options: DeployOptions, *, runner: ProcessRunner | None = None) -> int:
    """Run ``gcp-smoke`` against an existing deployment."""
    runner = runner or ProcessRunner()
    require_tools("gcloud", "gke-gcloud-auth-plugin", "terraform", "kubectl")
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-smoke-"))
    try:
        inputs = load_deployment_inputs(
            options.source,
            workdir,
            runner,
            input_fetch_env(options),
            environment=options.environment,
        )
        identities = _identities(options, inputs)
        with scoped_environment(identities.env("deploy")):
            infrastructure = read_infrastructure(runner, inputs)
            with scoped_environment(infrastructure.kube):
                passed = run_smoke(
                    runner,
                    {},
                    user_email=inputs.settings.post_deploy_smoke.user_email,  # type: ignore[attr-defined]
                    run_id=options.run_id or new_run_id(),
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0 if passed else SMOKE_FAILED_EXIT_STATUS


def report_failure(exc: BaseException) -> int:
    """Print a command failure without a traceback and return exit status 1."""
    error(str(exc) or type(exc).__name__)
    return 1
