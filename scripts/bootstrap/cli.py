"""Command-line wiring for the bootstrap deployment CLI."""

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from installation.loader import default_root_config_path

from account_recovery import account_recovery
from aws_bootstrap import BootstrapConfig, bootstrap_account
from aws_eks import IdentityFiles, deploy_eks, teardown_eks
from bootstrap_core import (
    AWS_ENVIRONMENTS,
    HELP_AWS_PROFILE,
    HELP_DRY_RUN,
    GDCBootstrapConfig,
    RepositoryIdentityError,
    code_block,
    confirm,
    error,
    get_default_gdc_project_id,
    header,
    info,
    resolve_github_repository,
    run_cmd,
    set_assume_yes,
    success,
    warn,
)
from gcp_base_images import (
    BaseImageError,
    import_selection,
    newest_publications,
    range_images_from_imports,
    render_range_image_env,
    resolve_selection,
)
from gcp_cli import LIFECYCLE_COMMANDS, add_lifecycle_subparsers, dispatch_lifecycle
from gcp_config_store import write_range_images
from gcp_control_plane import gdc_bootstrap_cluster
from gcp_foundation import bootstrap_gcp_foundation
from preflight import Cloud, Mode, preflight_gate
from terraform_deploy import terraform_deploy
from walkthrough import (
    walkthrough_acm_validation,
    walkthrough_backend_config,
    walkthrough_cognito_user,
    walkthrough_dns_setup,
    walkthrough_final_steps,
    walkthrough_github_secrets,
)

try:
    from runner import get_runner_config, provision_and_register_runners

    RUNNER_AVAILABLE = True
except ImportError:
    RUNNER_AVAILABLE = False

try:
    from gcp_runner import get_gcp_runner_config, provision_and_register_gcp_runners

    GCP_RUNNER_AVAILABLE = True
except ImportError:
    GCP_RUNNER_AVAILABLE = False

HELP_HEADLESS = "Non-interactive preflight: fail on missing prerequisites without prompting (auto-detected off a TTY)"
HELP_YES = (
    "Assume 'yes' for routine confirmation prompts so the bootstrap can run without a TTY "
    "(issue #1639). Does NOT authorize destructive cleanup; the leftover sweep has its own opt-in."
)
_AWS_COMPONENTS = ("core", "range", "portal")
_PREFLIGHT_COMPONENTS = (*_AWS_COMPONENTS, "deploy")


def full_deployment(env: str, profile: str, dry_run: bool = False) -> None:
    """Run complete deployment with interactive walkthrough."""
    header(f"Full {env.upper()} Deployment")

    print("""
This will guide you through a complete BigRAE deployment:

  1. Bootstrap AWS account (S3 state bucket with native locking, GitHub OIDC, IAM)
  2. Configure GitHub secrets (automated with gh CLI or manual)
  3. Write instance Terraform backend configuration (outside the repo)
  4. Set up GitHub Actions runners (optional - for self-hosted CI/CD)
  5. Deploy infrastructure (Core → Range → Portal)
  6. Configure DNS and SSL certificate (manual - external DNS)
  7. Create first user

Automated steps will ask for confirmation:
  [y] yes - run automatically
  [n] no - abort (all steps are required)
  [m] manual - show instructions and wait

Estimated time: 30-45 minutes (mostly waiting for RDS and ACM)
""")

    config = BootstrapConfig(env=env, **resolve_github_repository())
    if not dry_run and not confirm("Ready to begin?"):
        warn("Deployment cancelled")
        return

    if dry_run:
        info("[DRY-RUN] Showing what would happen...")

    # Phase 1: Bootstrap
    bootstrap_result = bootstrap_account(config, profile, dry_run=dry_run)

    # Phase 2: GitHub Secrets
    walkthrough_github_secrets(bootstrap_result, dry_run=dry_run)

    # Phase 3: Backend Configuration
    walkthrough_backend_config(bootstrap_result, dry_run=dry_run)

    # Phase 4: GitHub Actions Runner Setup (automated - issue #1433)
    if RUNNER_AVAILABLE:
        runner_config = get_runner_config(
            env=env,
            region=config.region,
            github_org=config.github_org,
            github_repo=config.github_repo,
            aws_profile=profile,
        )
        provision_and_register_runners(
            runner_config,
            dry_run=dry_run,
            bucket_name=bootstrap_result.get("bucket_name"),
        )
    else:
        warn("Runner module not available - skipping GitHub runner setup")

    # Phase 5: Terraform Deployment
    if not dry_run and not confirm("Continue with Terraform deployment?"):
        print("\nYou can resume later with:")
        code_block(f"./scripts/bootstrap/deploy.py terraform --env {env} --profile {profile}")
        return

    outputs = terraform_deploy(
        env,
        profile,
        dry_run=dry_run,
        bucket_name=bootstrap_result.get("bucket_name"),
    )

    if not dry_run and outputs:
        # Phase 6: ACM Validation
        walkthrough_acm_validation(outputs, dry_run=dry_run)

        # Phase 7: DNS Setup
        walkthrough_dns_setup(outputs, dry_run=dry_run)

        # Phase 8: First User
        walkthrough_cognito_user(outputs, env, profile, dry_run=dry_run)

    # Final Summary
    walkthrough_final_steps(env)


def runners_deployment(
    env: str,
    profile: str,
    dry_run: bool = False,
    use_existing_network: bool = False,
    runner_count: int | None = None,
) -> None:
    """Provision + register self-hosted GitHub runners (issue #1433).

    First-class automatable path: Terraform provisions the runner fleet and (by
    default) a dedicated ADR-004-R20-compliant runner VPC, then each runner is
    registered over SSM and verified via the GitHub runners API. Registration
    tokens are minted per runner and never persisted (see runner.py).
    """
    if not RUNNER_AVAILABLE:
        error("Runner module not available - cannot provision runners")
        sys.exit(1)

    config = BootstrapConfig(env=env, **resolve_github_repository())
    runner_config = get_runner_config(
        env=env,
        region=config.region,
        github_org=config.github_org,
        github_repo=config.github_repo,
        aws_profile=profile,
    )
    provision_and_register_runners(
        runner_config,
        dry_run=dry_run,
        use_existing_network=use_existing_network,
        runner_count=runner_count,
    )


def gcp_runners_deployment(
    env: str,
    project_id: str,
    region: str,
    zone: str,
    *,
    dry_run: bool = False,
    runner_count: int | None = None,
    labels: str | None = None,
) -> None:
    """Provision + register GCP-native self-hosted runners (issue #1546).

    The GCP counterpart to :func:`runners_deployment`: Terraform provisions the
    GCE fleet plus a mandatory dedicated custom VPC (ADR-008-R8, no opt-out),
    then each runner is registered over ``gcloud compute ssh --tunnel-through-iap``
    with a per-runner token delivered over stdin and verified online + labeled via
    the GitHub runners API. Tokens are minted per runner and never persisted (see
    gcp_runner.py). Uses the operator's default gcloud/ADC identity (no profile).
    """
    if not GCP_RUNNER_AVAILABLE:
        error("GCP runner module not available - cannot provision GCP runners")
        sys.exit(1)
    if not project_id:
        error("GCP runner provisioning requires --project-id (or PANW_GCP_DEV / repo-root .env)")
        sys.exit(1)

    # Resolve the repository without treating the GCP environment as an AWS tier.
    repository = resolve_github_repository()
    runner_config = get_gcp_runner_config(
        env=env,
        project_id=project_id,
        region=region,
        zone=zone,
        github_org=repository["github_org"],
        github_repo=repository["github_repo"],
        labels=labels,
    )
    provision_and_register_gcp_runners(
        runner_config,
        dry_run=dry_run,
        runner_count=runner_count,
    )


def _guest_base_images(release_manifest: str) -> dict[str, str]:
    """Return the guest images that the release manifest names."""
    from installation.errors import InstallationConfigError
    from installation.gcp_release import load_release_manifest

    selected = release_manifest or os.environ.get("BIGRAE_RELEASE_MANIFEST", "")
    if not selected:
        raise BaseImageError("a release manifest is required; pass --release-manifest or use the deployer image")
    try:
        return dict(load_release_manifest(Path(selected)).base_images)
    except InstallationConfigError as exc:
        raise BaseImageError(str(exc)) from None


def _guest_trust(store_trust: object | None, repository_id: str, owner_id: str) -> tuple[str, str]:
    """Resolve the signer trust from the flags or from ``settings.release_trust`` of the bundle."""
    configured = (
        (str(store_trust.github_repository_id), str(store_trust.github_owner_id))  # type: ignore[attr-defined]
        if store_trust is not None
        else ("", "")
    )
    if repository_id or owner_id:
        if store_trust is not None and (repository_id, owner_id) != configured:
            raise BaseImageError("the trusted repository flags conflict with settings.release_trust")
        configured = (repository_id, owner_id)
    if not all(configured):
        raise BaseImageError(
            "guest image verification needs settings.release_trust in the bundle, "
            "or --trusted-repository-id and --trusted-owner-id"
        )
    return configured


def _no_cloud_token() -> str:
    """Refuse a cloud token: the public release registry is read anonymously."""
    raise BaseImageError("the release registry must be readable without cloud credentials")


def print_release_base_images(registry: str, repository_id: str, owner_id: str) -> dict[str, str]:
    """Print the newest verified guest image of each role, for a release to record (#2679, #2668).

    It needs no GCP project. Standard output carries only the JSON, so the release
    workflow can parse it; the progress lines go to standard error.
    """
    from gcp_oci import OciClient
    from gcp_provenance import default_verifier

    repository_id, owner_id = _guest_trust(None, repository_id, owner_id)
    with contextlib.redirect_stdout(sys.stderr):
        newest = newest_publications(registry)
        resolve_selection(
            newest,
            repository_id=repository_id,
            owner_id=owner_id,
            oci=OciClient(_no_cloud_token),
            verifier=default_verifier(),
        )
    print(json.dumps(newest, indent=2, sort_keys=True))
    return newest


def gcp_base_images_import(
    project_id: str,
    region: str,
    *,
    store: str = "",
    release_manifest: str = "",
    release_source: str = "",
    trust: tuple[str, str] = ("", ""),
    dry_run: bool = False,
) -> dict[str, str]:
    """Verify and import the guest images that a release names, and record their references.

    ``trust`` holds the signing repository and owner IDs from the command flags; with
    ``store`` they must match ``settings.release_trust`` of the bundle.

    Every role is verified before any change: the digest-verified OCI manifest, the artifact
    contract, and a Sigstore bundle that satisfies the trust anchor at the build revision,
    from a protected build branch (#2668). The import holds the deployment lock of
    ``store``, reuses unchanged images, and writes ``range-images.json`` after every role is
    ready; without ``store`` it prints the references. No GitHub service is used.
    """
    from gcp_inputs import load_deployment_inputs, private_workdir
    from gcp_lock import acquire_lock, release_lock
    from gcp_oci import OciClient
    from gcp_prereqs import access_token
    from gcp_process import ProcessRunner
    from gcp_provenance import default_verifier

    if not project_id:
        error("gcp-images requires --project-id (the range project the images import into)")
        sys.exit(1)
    base_images = _guest_base_images(release_manifest)
    runner = ProcessRunner()
    with tempfile.TemporaryDirectory(prefix="bigrae-gcp-images-") as scratch:
        workdir = private_workdir(Path(scratch))
        store_trust = None
        if store:
            inputs = load_deployment_inputs(store, workdir, runner, {})
            store_trust = inputs.settings.release_trust  # type: ignore[attr-defined]
        repository_id, owner_id = _guest_trust(store_trust, *trust)
        artifacts = resolve_selection(
            base_images,
            repository_id=repository_id,
            owner_id=owner_id,
            oci=OciClient(lambda: access_token(runner, {})),
            verifier=default_verifier(),
            release_source=release_source,
        )
        _validate_base_image_auth()
        lock = None
        if store and not dry_run:
            lock = acquire_lock(runner, store, {}, command="gcp-images", run_id=uuid.uuid4().hex, workdir=workdir)
        try:
            imported = import_selection(artifacts, project=project_id, region=region, dry_run=dry_run)
            images = range_images_from_imports(render_range_image_env(imported))
            if dry_run:
                info("[DRY-RUN] Would record range images: " + ", ".join(sorted(images)))
                return images
            if store:
                write_range_images(store, images)
                success(f"Recorded {', '.join(sorted(images))} in {store}")
            else:
                print(json.dumps(images, indent=2, sort_keys=True))
            return images
        finally:
            if lock is not None:
                release_lock(runner, lock, {})


def _validate_base_image_auth(*, command_runner: Callable[..., object] = run_cmd) -> None:
    """Validate the existing gcloud session without mutation."""
    result = command_runner(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], check=False, capture=True
    )
    if result is None or getattr(result, "returncode", 1) != 0 or not (getattr(result, "stdout", "") or "").strip():
        raise BaseImageError("gcloud authentication is required before importing base images")


def _missing_dependency_lines(commands: dict[str, str]) -> list[str]:
    """Return formatted '  - cmd: desc' lines for each command not found on PATH."""
    return [f"  - {cmd}: {desc}" for cmd, desc in commands.items() if not shutil.which(cmd)]


# Tool -> install-hint. `_required_tools` selects the subset a command needs.
_TOOL_HINTS = {
    "git": "Git - https://git-scm.com/downloads",
    "aws": "AWS CLI - https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
    "terraform": "Terraform - https://developer.hashicorp.com/terraform/downloads",
    "gcloud": "Google Cloud CLI - https://cloud.google.com/sdk/docs/install",
    "gh": "GitHub CLI - https://cli.github.com/",
    "ssh-keygen": "OpenSSH client tools - https://www.openssh.com/",
    "docker": "Docker - https://docs.docker.com/engine/install/",
    "kubectl": "kubectl - https://kubernetes.io/docs/tasks/tools/",
    "helm": "Helm - https://helm.sh/docs/intro/install/",
    "oras": "ORAS CLI - https://oras.land/docs/installation",
}


def _required_tools(command: str | None, cloud: str | None) -> set[str]:
    """Return the set of required CLI tools for a bootstrap command."""
    if command == "gcp-foundation":
        return {"gcloud", "terraform"}
    if command == "gcp-images":
        return {"gcloud", "oras"}
    if command == "gcp-images-verify":
        # Verification reads the registry and Sigstore in-process; it runs no tool.
        return set()
    if command == "gdc-bootstrap":
        return {"gcloud", "ssh-keygen"}
    if command == "runners":
        # GCP runners use gcloud + ADC (no AWS CLI); both clouds need gh + terraform.
        cloud_tools = {"gcloud"} if cloud == Cloud.GCP.value else {"aws"}
        return {"git", "gh", "terraform"} | cloud_tools
    if command in {"eks-deploy", "eks-teardown"}:
        return {"git", "aws", "terraform", "kubectl", "helm"}
    if command in {"bootstrap", "full"}:
        # ADR-069: gh resolves the target repository identity.
        return {"git", "aws", "terraform", "gh"}
    if command in {None, "terraform"}:
        return {"git", "aws", "terraform"}
    return {"git"}


# GCP commands that never use GitHub (#2571); they get no gh recommendation.
_GITHUB_FREE_COMMANDS = frozenset({"gcp-foundation", "gcp-images", "gcp-images-verify", "gdc-bootstrap"})


def check_dependencies(
    command: str | None = None,
    cloud: str | None = None,
) -> None:
    """Check command-specific dependencies before starting."""
    required = {tool: _TOOL_HINTS[tool] for tool in _required_tools(command, cloud)}
    optional = (
        {}
        if command in _GITHUB_FREE_COMMANDS
        else {"gh": "GitHub CLI - https://cli.github.com/ (recommended for automating GitHub secrets)"}
    )

    missing_required = _missing_dependency_lines(required)
    missing_optional = _missing_dependency_lines(optional)

    if missing_required:
        error("Missing required dependencies:")
        for item in missing_required:
            print(item)
        sys.exit(1)

    if missing_optional:
        warn("Missing optional dependencies (some automation features will be unavailable):")
        for item in missing_optional:
            print(item)
        print()


def _add_runners_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the `runners` subcommand (issue #1433 AWS; issue #1546 GCP).

    --cloud selects the provider; AWS is the default for back-compat. GCP
    provisions into the target GCP project (dev-tenant containment) with a
    dedicated runner VPC and IAP-only registration.
    """
    runners_parser = subparsers.add_parser(
        "runners",
        help="Provision and auto-register self-hosted GitHub Actions runners (dedicated runner VPC by default)",
    )
    runners_parser.add_argument(
        "--cloud", choices=[c.value for c in Cloud], default=Cloud.AWS.value, help="Target cloud (default: aws)"
    )
    runners_parser.add_argument(
        "--env",
        required=True,
        help="Environment (AWS: dev/prod; GCP: e.g. gcp-dev). Validated per --cloud.",
    )
    runners_parser.add_argument("--profile", help=HELP_AWS_PROFILE + " (AWS only)")
    runners_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    runners_parser.add_argument(
        "--use-existing-network",
        action="store_true",
        help=(
            "Do not provision the standard dedicated runner network; use an existing compliant "
            "network's vpc_id/subnet_id from a gitignored local.auto.tfvars in the runner root."
        ),
    )
    runners_parser.add_argument(
        "--runner-count",
        type=int,
        default=None,
        help="Override runner_count for this apply (defaults to the runner tfvars value).",
    )
    # GCP-only flags (ignored for --cloud aws). GCP uses the operator's default
    # gcloud/ADC identity, so there is no --profile equivalent.
    runners_parser.add_argument(
        "--project-id",
        default=get_default_gdc_project_id(),
        help="GCP project ID to provision runners into (GCP only; defaults to PANW_GCP_DEV or repo-root .env)",
    )
    runners_parser.add_argument("--region", default="us-central1", help="GCP region (GCP only)")
    runners_parser.add_argument("--zone", default="us-central1-a", help="GCP compute zone (GCP only)")
    runners_parser.add_argument(
        "--labels",
        default=None,
        help="Custom runner label set (GCP only; defaults to the environment name, e.g. gcp-dev)",
    )


def _add_gdc_bootstrap_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire `gdc-bootstrap`: build the legacy GDC VM Runtime substrate only.

    The GKE control plane and the GCE range plane deploy through `gcp-deploy` (#2571).
    """
    gdc_parser = subparsers.add_parser(
        "gdc-bootstrap",
        help="Build the legacy Google Distributed Cloud VM Runtime substrate (GDC range backend only)",
    )
    gdc_parser.add_argument(
        "--project-id",
        default=get_default_gdc_project_id(),
        help="GCP project ID (defaults to PANW_GCP_DEV or repo-root .env)",
    )
    gdc_parser.add_argument(
        "--environment", default="gcp-dev", help="Deployment name that the substrate secrets use (default gcp-dev)"
    )
    gdc_parser.add_argument("--cluster-id", default="cluster1", help="Cluster name / prefix")
    gdc_parser.add_argument("--region", default="us-central1", help="Cluster region")
    gdc_parser.add_argument("--zone", default="us-central1-a", help="Compute Engine zone")
    gdc_parser.add_argument("--google-account-email", help="Optional Google identity to grant cluster-admin")
    gdc_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    gdc_parser.add_argument("--yes", action="store_true", help=HELP_YES)


def _add_gcp_images_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire `gcp-images`: verify and import the guest images that a release selects (#2309, #2668)."""
    images_parser = subparsers.add_parser(
        "gcp-images",
        help="Verify and import the guest images that a release names as native GCE images, and record them",
    )
    images_parser.add_argument("--project-id", default="", help="GCP project the range guest images import into")
    images_parser.add_argument(
        "--release-manifest",
        default="",
        help="Release manifest (default: the deployer image's, from BIGRAE_RELEASE_MANIFEST)",
    )
    images_parser.add_argument(
        "--release-source",
        default="",
        help="Mirror registry prefix that holds <prefix>/gce-<role>@<release digest>",
    )
    images_parser.add_argument(
        "--store",
        default="",
        help="Deployment config bucket prefix (gs://<bucket>[/<prefix>]): trust anchor, lock, and range-images.json",
    )
    images_parser.add_argument("--trusted-repository-id", default="", help="Immutable ID of the signing repository")
    images_parser.add_argument("--trusted-owner-id", default="", help="Immutable ID of the signing repository owner")
    images_parser.add_argument("--region", default="us-central1", help="Region for the temporary transfer bucket")
    images_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    images_parser.add_argument(
        "--resolve",
        action="store_true",
        help="Print the newest verified guest image of each role as JSON, for a release; import nothing",
    )
    images_parser.add_argument(
        "--registry", default="", help="With --resolve: the release registry prefix, ghcr.io/<owner>/<repository>"
    )


def _add_eks_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Add the explicit AWS EKS deploy and teardown commands."""
    deploy_parser = subparsers.add_parser(
        "eks-deploy",
        help="Explicitly deploy backend: aws to EKS through the shared Helm chart",
    )
    deploy_parser.add_argument("--config", help="Root installation config (default bigrae.yaml; legacy shifter.yaml)")
    deploy_parser.add_argument(
        "--images",
        default=".shifter/images.json",
        help="JSON mapping of workloads to attested repository@sha256 identities",
    )
    deploy_parser.add_argument(
        "--backend-config",
        default=os.environ.get("SHIFTER_BACKEND_CONFIG_PATH", ""),
        help="Protected S3 backend config rendered outside the repository",
    )
    deploy_parser.add_argument(
        "--terraform-inputs",
        default=os.environ.get("SHIFTER_EKS_TFVARS_PATH", ""),
        help="Protected JSON var-file containing the EKS infrastructure/runtime projection",
    )
    deploy_parser.add_argument(
        "--identity-cutover-facts", default="", help="Private 0600 identity cutover facts JSON file"
    )
    deploy_parser.add_argument(
        "--identity-bootstrap-facts", default="", help="Private 0600 identity bootstrap facts JSON file"
    )
    deploy_parser.add_argument(
        "--catalog-credential-file",
        default="",
        help="Private 0600 file with a current native bearer proof for catalog setup",
    )
    deploy_parser.add_argument("--profile", help=HELP_AWS_PROFILE)
    deploy_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)

    teardown_parser = subparsers.add_parser(
        "eks-teardown",
        help="Explicitly tear down only the root-configured AWS EKS bundle",
    )
    teardown_parser.add_argument("--config", help="Root installation config (default bigrae.yaml; legacy shifter.yaml)")
    teardown_parser.add_argument(
        "--backend-config",
        default=os.environ.get("SHIFTER_BACKEND_CONFIG_PATH", ""),
        help="Protected S3 backend config rendered outside the repository",
    )
    teardown_parser.add_argument(
        "--terraform-inputs",
        default=os.environ.get("SHIFTER_EKS_TFVARS_PATH", ""),
        help="Protected JSON var-file containing the isolated EKS root inputs",
    )
    teardown_parser.add_argument("--profile", help=HELP_AWS_PROFILE)
    teardown_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)


def _add_aws_deploy_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Add the AWS bootstrap, Terraform, and full-deploy commands."""
    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Bootstrap AWS account (S3 state bucket with native locking, GitHub OIDC, IAM)"
    )
    bootstrap_parser.add_argument("--env", required=True, choices=AWS_ENVIRONMENTS, help="Environment")
    bootstrap_parser.add_argument("--profile", required=True, help=HELP_AWS_PROFILE)
    bootstrap_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    bootstrap_parser.add_argument("--headless", action="store_const", const=True, default=None, help=HELP_HEADLESS)
    bootstrap_parser.add_argument("--yes", action="store_true", help=HELP_YES)

    terraform_parser = subparsers.add_parser("terraform", help="Deploy Terraform infrastructure")
    terraform_parser.add_argument("--env", required=True, choices=AWS_ENVIRONMENTS, help="Environment")
    terraform_parser.add_argument("--profile", required=True, help=HELP_AWS_PROFILE)
    terraform_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    terraform_parser.add_argument("--headless", action="store_const", const=True, default=None, help=HELP_HEADLESS)
    terraform_parser.add_argument("--yes", action="store_true", help=HELP_YES)

    full_parser = subparsers.add_parser("full", help="Full interactive deployment (bootstrap + config + terraform)")
    full_parser.add_argument("--env", required=True, choices=AWS_ENVIRONMENTS, help="Environment")
    full_parser.add_argument("--profile", required=True, help=HELP_AWS_PROFILE)
    full_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    full_parser.add_argument("--headless", action="store_const", const=True, default=None, help=HELP_HEADLESS)
    full_parser.add_argument("--yes", action="store_true", help=HELP_YES)


def _add_preflight_and_recovery_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Add prerequisite validation and account-leftover recovery commands."""
    preflight_parser = subparsers.add_parser(
        "preflight", help="Validate deploy prerequisites (tools, secrets, config) without making changes"
    )
    preflight_parser.add_argument("--cloud", required=True, choices=[c.value for c in Cloud], help="Target cloud")
    preflight_parser.add_argument("--env", required=True, help="Environment (e.g. dev, prod, gcp-dev)")
    preflight_parser.add_argument(
        "--component",
        choices=sorted(_PREFLIGHT_COMPONENTS),
        default=None,
        help="Deployment component or protected-environment boundary to validate",
    )
    preflight_parser.add_argument("--headless", action="store_const", const=True, default=None, help=HELP_HEADLESS)
    gcp_inputs = preflight_parser.add_mutually_exclusive_group()
    gcp_inputs.add_argument("--store", default="", help="GCP only: deployment config bucket prefix to validate")
    gcp_inputs.add_argument("--inputs", default="", help="GCP only: local deployment input directory to validate")

    recovery_parser = subparsers.add_parser(
        "account-recovery",
        help="Detect (and optionally --sweep) leftover AWS resources from an incomplete prior teardown",
    )
    recovery_parser.add_argument("--env", required=True, choices=AWS_ENVIRONMENTS, help="Environment")
    recovery_parser.add_argument("--profile", required=True, help=HELP_AWS_PROFILE)
    recovery_parser.add_argument(
        "--sweep",
        action="store_true",
        help="Delete the owned leftovers (explicit destructive opt-in; detection is read-only without it)",
    )
    recovery_parser.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    recovery_parser.add_argument("--yes", action="store_true", help=HELP_YES)


def _build_parser() -> argparse.ArgumentParser:
    """Build the bootstrap CLI argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        description="Shifter deployment CLI - interactive deployment guide",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview full deployment (no changes)
  ./scripts/bootstrap/deploy.py full --env prod --profile my-prod-profile --dry-run

  # Run full interactive deployment
  ./scripts/bootstrap/deploy.py full --env prod --profile my-prod-profile

  # Just bootstrap AWS account
  ./scripts/bootstrap/deploy.py bootstrap --env prod --profile my-prod-profile

  # Just run terraform (after bootstrap)
  ./scripts/bootstrap/deploy.py terraform --env prod --profile my-prod-profile

  # Bootstrap a repeatable Google Distributed Cloud VM Runtime cluster
  ./scripts/bootstrap/deploy.py gdc-bootstrap --project-id example-gcp-project --cluster-id cluster1
        """,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_eks_subparsers(subparsers)
    _add_aws_deploy_subparsers(subparsers)
    _add_preflight_and_recovery_subparsers(subparsers)
    _add_runners_subparser(subparsers)
    _add_gdc_bootstrap_subparser(subparsers)
    _add_gcp_images_subparser(subparsers)
    add_lifecycle_subparsers(subparsers)
    foundation = subparsers.add_parser(
        "gcp-foundation",
        help="Bootstrap GCP state, purpose identities, the deployment config bucket, and the image network",
    )
    foundation.add_argument(
        "--inputs",
        required=True,
        help="Explicit foundation JSON Terraform var-file; omit purpose_contexts for the GitHub-less mode",
    )
    foundation.add_argument("--dry-run", action="store_true", help=HELP_DRY_RUN)
    foundation.add_argument("--yes", action="store_true", help=HELP_YES)

    return parser


def _build_gdc_bootstrap_config(args: argparse.Namespace) -> GDCBootstrapConfig:
    """Build the GDCBootstrapConfig for the `gdc-bootstrap` subcommand from parsed args."""
    return GDCBootstrapConfig(
        project_id=args.project_id,
        environment=args.environment,
        cluster_id=args.cluster_id,
        region=args.region,
        zone=args.zone,
        google_account_email=args.google_account_email,
        range_backend="gdc",
    )


def _dispatch_runners(args: argparse.Namespace) -> None:
    """Dispatch the `runners` subcommand to the AWS or GCP provisioning path."""
    if args.cloud == Cloud.GCP.value:
        # The dedicated runner network is mandatory for GCP (ADR-008-R8), so the
        # AWS-only existing-network opt-out is rejected rather than silently ignored.
        if args.use_existing_network:
            error("--use-existing-network is not supported for --cloud gcp; the dedicated runner network is mandatory")
            sys.exit(1)
        # gcp_runner._verify_prerequisites fails closed on gh auth + gcloud ADC
        # before any mutation. The full platform preflight_gate is intentionally
        # not used here: a runner standup must not require the platform
        # environment's secret/config surface.
        gcp_runners_deployment(
            args.env,
            args.project_id,
            args.region,
            args.zone,
            dry_run=args.dry_run,
            runner_count=args.runner_count,
            labels=args.labels,
        )
        return

    if args.env not in AWS_ENVIRONMENTS:
        error(f"--env must be one of {', '.join(AWS_ENVIRONMENTS)} for --cloud aws (got '{args.env}')")
        sys.exit(1)
    if not args.profile:
        error("--profile is required for --cloud aws")
        sys.exit(1)
    runners_deployment(
        args.env,
        args.profile,
        dry_run=args.dry_run,
        use_existing_network=args.use_existing_network,
        runner_count=args.runner_count,
    )


def _handle_preflight(args: argparse.Namespace) -> None:
    """Run the read-only prerequisite gate."""
    preflight_gate(
        Cloud(args.cloud),
        Mode.LOCAL,
        args.env,
        component=args.component,
        headless=args.headless,
        gcp_inputs=args.store or args.inputs,
    )


def _handle_account_recovery(args: argparse.Namespace) -> None:
    """Run account-leftover detection or its explicitly authorized sweep."""
    account_recovery(args.env, args.profile, sweep=args.sweep, dry_run=args.dry_run)


def _handle_eks_deploy(args: argparse.Namespace) -> None:
    """Deploy the root-configured AWS EKS bundle."""
    deploy_eks(
        str(args.config or default_root_config_path()),
        args.images,
        backend_config_path=args.backend_config,
        terraform_inputs_path=args.terraform_inputs,
        identity_files=IdentityFiles(
            cutover_facts=args.identity_cutover_facts or None,
            bootstrap_facts=args.identity_bootstrap_facts or None,
            catalog_credential=args.catalog_credential_file or None,
        ),
        aws_profile=args.profile,
        dry_run=args.dry_run,
    )


def _handle_eks_teardown(args: argparse.Namespace) -> None:
    """Tear down only the root-configured AWS EKS bundle."""
    teardown_eks(
        str(args.config or default_root_config_path()),
        backend_config_path=args.backend_config,
        terraform_inputs_path=args.terraform_inputs,
        aws_profile=args.profile,
        dry_run=args.dry_run,
    )


def _handle_bootstrap(args: argparse.Namespace) -> None:
    """Bootstrap the selected AWS account and render operator handoffs."""
    config = BootstrapConfig(env=args.env, **resolve_github_repository())
    result = bootstrap_account(config, args.profile, dry_run=args.dry_run)
    if not args.dry_run:
        walkthrough_github_secrets(result, dry_run=False)
        walkthrough_backend_config(result, dry_run=False)


def _handle_terraform(args: argparse.Namespace) -> None:
    """Deploy AWS Terraform and complete its operator walkthroughs."""
    outputs = terraform_deploy(args.env, args.profile, dry_run=args.dry_run)
    if not args.dry_run and outputs:
        walkthrough_acm_validation(outputs, dry_run=False)
        walkthrough_dns_setup(outputs, dry_run=False)
        walkthrough_cognito_user(outputs, args.env, args.profile, dry_run=False)
        walkthrough_final_steps(args.env)


def _handle_full(args: argparse.Namespace) -> None:
    """Run the complete AWS deployment workflow."""
    full_deployment(args.env, args.profile, dry_run=args.dry_run)


def _handle_gdc_bootstrap(
    args: argparse.Namespace,
    *,
    bootstrapper: Callable[..., object] | None = None,
) -> None:
    """Build the legacy GDC VM Runtime substrate."""
    (bootstrapper or gdc_bootstrap_cluster)(_build_gdc_bootstrap_config(args), dry_run=args.dry_run)


_COMMAND_HANDLERS = {
    "gcp-foundation": lambda args: bootstrap_gcp_foundation(args.inputs, dry_run=args.dry_run),
    "preflight": _handle_preflight,
    "account-recovery": _handle_account_recovery,
    "eks-deploy": _handle_eks_deploy,
    "eks-teardown": _handle_eks_teardown,
    "bootstrap": _handle_bootstrap,
    "terraform": _handle_terraform,
    "full": _handle_full,
    "runners": _dispatch_runners,
    "gdc-bootstrap": _handle_gdc_bootstrap,
    "gcp-images": lambda args: (
        print_release_base_images(args.registry, args.trusted_repository_id, args.trusted_owner_id)
        if args.resolve
        else gcp_base_images_import(
            args.project_id,
            args.region,
            store=args.store,
            release_manifest=args.release_manifest,
            release_source=args.release_source,
            trust=(args.trusted_repository_id, args.trusted_owner_id),
            dry_run=args.dry_run,
        )
    ),
}


def main(
    *,
    dependency_checker: Callable[..., None] | None = None,
    gdc_bootstrapper: Callable[..., None] | None = None,
) -> None:
    """Parse arguments and dispatch through injectable external operations."""
    args = _build_parser().parse_args()
    if args.command in LIFECYCLE_COMMANDS:
        # GitHub-less lifecycle commands check their own tools and never need gh or git.
        dispatch_lifecycle(args)
        return
    if getattr(args, "yes", False):
        set_assume_yes(True)
    tools_for = "gcp-images-verify" if getattr(args, "verify_only", False) else args.command
    (dependency_checker or check_dependencies)(tools_for, cloud=getattr(args, "cloud", None))
    if args.command in {"bootstrap", "terraform", "full"}:
        preflight_gate(Cloud.AWS, Mode.LOCAL, args.env, headless=args.headless)
    try:
        handler = _COMMAND_HANDLERS[args.command]
        if args.command == "gdc-bootstrap" and gdc_bootstrapper is not None:
            _handle_gdc_bootstrap(args, bootstrapper=gdc_bootstrapper)
        else:
            handler(args)
    except (BaseImageError, RepositoryIdentityError) as exc:
        error(str(exc))
        raise SystemExit(1) from None
