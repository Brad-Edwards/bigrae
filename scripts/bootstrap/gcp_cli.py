"""Argument parsing and dispatch for the GitHub-less GCP lifecycle commands (#2571).

Commands:

- ``gcp-deploy``: stand up or update one deployment from a published release;
- ``gcp-release-scan``: scan the exact images of a release manifest;
- ``gcp-smoke``: run the advisory post-deploy range smoke;
- ``gcp-destroy``: tear down the platform resources of one deployment;
- ``gcp-config``: publish, pull, and show the deployment inputs, and set range images.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from gcp_config_store import publish, pull, show, write_range_images
from gcp_deploy import (
    STAGES,
    DeployOptions,
    ScanOptions,
    report_failure,
    run_deploy,
    run_release_scan_command,
    run_smoke_command,
)
from gcp_destroy import DestroyOptions, run_destroy
from gcp_inputs import RANGE_IMAGE_ROLES

LIFECYCLE_COMMANDS = frozenset({"gcp-deploy", "gcp-release-scan", "gcp-smoke", "gcp-destroy", "gcp-config"})
_IDENTITY_HELP = "impersonate (operator, default) or ambient (the caller already is the purpose identity, CI)"
_STORE_HELP = "Deployment config bucket prefix"


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the input source, deployment name, and identity arguments."""
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--store", help=f"{_STORE_HELP}, gs://<bucket>[/<prefix>]")
    source.add_argument("--inputs", help="Local deployment input directory (CI adapter output or a draft bundle)")
    parser.add_argument("--environment", default="", help="Deployment name (default: deployment.name)")
    parser.add_argument("--identity", choices=("impersonate", "ambient"), default="impersonate", help=_IDENTITY_HELP)
    parser.add_argument(
        "--name-prefix", default="", help="Purpose identity name prefix (default shifter-<environment>)"
    )


def _add_lock_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the deployment lock arguments."""
    parser.add_argument(
        "--lock", choices=("gcs", "external"), default="gcs", help="gcs (config bucket lock) or external (CI)"
    )
    parser.add_argument("--break-lock", action="store_true", help="Remove a stale deployment lock first")


def _add_trust_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the release manifest and provenance trust arguments."""
    parser.add_argument("--release-manifest", default="", help="Release manifest (default: the deployer image's)")
    parser.add_argument("--trusted-repository-id", default="", help="CI only: the running repository ID")
    parser.add_argument("--trusted-owner-id", default="", help="CI only: the running repository owner ID")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the evidence run arguments."""
    parser.add_argument("--run-id", default="", help="Evidence run id (default: a generated id)")
    parser.add_argument("--attempt", type=int, default=1, help="Evidence run attempt")
    parser.add_argument(
        "--evidence-bucket", default="", help="Release-evidence bucket (default <project>-release-evidence)"
    )


def add_lifecycle_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Wire the GitHub-less GCP lifecycle subcommands."""
    deploy = subparsers.add_parser("gcp-deploy", help="Stand up or update a GCP deployment from a published release")
    _add_source_arguments(deploy)
    _add_lock_arguments(deploy)
    _add_trust_arguments(deploy)
    _add_run_arguments(deploy)
    deploy.add_argument("--stage", choices=STAGES, default="all", help="Run all stages, or one CI stage")
    deploy.add_argument("--deploy-service-account", default="", help="Deploy identity (default: derived)")
    deploy.add_argument("--release-scan-service-account", default="", help="Release-scan identity (default: derived)")
    deploy.add_argument("--first-install", action="store_true", help="Accept an inactive certificate before DNS exists")
    deploy.add_argument(
        "--allow-missing-range-images", action="store_true", help="Deploy the platform before range images exist"
    )
    deploy.add_argument(
        "--reconcile-foundation-image-network", action="store_true", help="Run the narrow foundation reconcile"
    )
    deploy.add_argument("--no-smoke", action="store_true", help="Skip the advisory post-deploy smoke")
    deploy.add_argument("--operator-password-env", default="", help="CI only: env variable with the operator password")
    deploy.add_argument(
        "--image-roots-output", default="", help="CI only: write the deployment image roots (JSON) to this file"
    )

    scan = subparsers.add_parser("gcp-release-scan", help="Scan the exact images of a release manifest")
    scan.add_argument("--release-manifest", required=True, help="Release manifest to scan")
    scan.add_argument("--project-id", required=True, help="Deployment project")
    scan.add_argument("--environment", required=True, help="Deployment name")
    scan.add_argument("--identity", choices=("impersonate", "ambient"), default="impersonate", help=_IDENTITY_HELP)
    scan.add_argument("--name-prefix", default="", help="Purpose identity name prefix (default shifter-<environment>)")
    scan.add_argument("--release-scan-service-account", default="", help="Release-scan identity (default: derived)")
    _add_run_arguments(scan)

    smoke = subparsers.add_parser("gcp-smoke", help="Run the advisory post-deploy range smoke")
    _add_source_arguments(smoke)
    smoke.add_argument("--deploy-service-account", default="", help="Deploy identity (default: derived)")
    smoke.add_argument("--run-id", default="", help="Smoke run id (default: a generated id)")

    destroy = subparsers.add_parser("gcp-destroy", help="Tear down the platform resources of a GCP deployment")
    _add_source_arguments(destroy)
    _add_lock_arguments(destroy)
    _add_trust_arguments(destroy)
    destroy.add_argument("--confirm", required=True, help="The deployment name, typed to confirm the teardown")
    destroy.add_argument("--destroy-service-account", default="", help="Destroy identity (default: derived)")

    config = subparsers.add_parser("gcp-config", help="Manage the deployment config bucket")
    actions = config.add_subparsers(dest="config_action", required=True)
    publish_parser = actions.add_parser("publish", help="Validate a local bundle and upload it")
    publish_parser.add_argument("--store", required=True, help=_STORE_HELP)
    publish_parser.add_argument("--inputs", required=True, help="Local bundle directory")
    pull_parser = actions.add_parser("pull", help="Download the bundle into a private directory")
    pull_parser.add_argument("--store", required=True, help=_STORE_HELP)
    pull_parser.add_argument("--output", required=True, help="Target directory")
    show_parser = actions.add_parser("show", help="Validate the stored bundle and print a summary")
    show_parser.add_argument("--store", required=True, help=_STORE_HELP)
    image_parser = actions.add_parser("set-range-image", help="Record one exact range guest image reference")
    image_parser.add_argument("--store", required=True, help=_STORE_HELP)
    image_parser.add_argument("--role", required=True, choices=RANGE_IMAGE_ROLES, help="Range guest role")
    image_parser.add_argument("--image", required=True, help="projects/<project>/global/images/[family/]<name>")


def _deploy_options(args: argparse.Namespace) -> DeployOptions:
    """Build ``gcp-deploy`` or ``gcp-smoke`` options; an argument that the command lacks takes its default."""
    return DeployOptions(
        source=args.store or args.inputs,
        environment=args.environment,
        release_manifest=getattr(args, "release_manifest", ""),
        stage=getattr(args, "stage", "all"),
        identity_mode=args.identity,
        name_prefix=args.name_prefix,
        deploy_service_account=getattr(args, "deploy_service_account", ""),
        release_scan_service_account=getattr(args, "release_scan_service_account", ""),
        lock=getattr(args, "lock", "external"),
        break_lock=getattr(args, "break_lock", False),
        first_install=getattr(args, "first_install", False),
        allow_missing_range_images=getattr(args, "allow_missing_range_images", False),
        reconcile_foundation_image_network=getattr(args, "reconcile_foundation_image_network", False),
        smoke=not getattr(args, "no_smoke", False),
        operator_password_env=getattr(args, "operator_password_env", ""),
        trusted_repository_id=getattr(args, "trusted_repository_id", ""),
        trusted_owner_id=getattr(args, "trusted_owner_id", ""),
        run_id=getattr(args, "run_id", ""),
        attempt=getattr(args, "attempt", 1),
        evidence_bucket=getattr(args, "evidence_bucket", ""),
        image_roots_output=getattr(args, "image_roots_output", ""),
    )


def _handle_config(args: argparse.Namespace) -> int:
    """Run one ``gcp-config`` action."""
    if args.config_action == "publish":
        publish(args.store, args.inputs)
    elif args.config_action == "pull":
        pull(args.store, args.output)
    elif args.config_action == "show":
        show(args.store)
    else:
        write_range_images(args.store, {args.role: args.image})
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "gcp-deploy": lambda args: run_deploy(_deploy_options(args)),
    "gcp-release-scan": lambda args: run_release_scan_command(
        ScanOptions(
            release_manifest=args.release_manifest,
            project_id=args.project_id,
            environment=args.environment,
            identity_mode=args.identity,
            name_prefix=args.name_prefix,
            release_scan_service_account=args.release_scan_service_account,
            evidence_bucket=args.evidence_bucket,
            run_id=args.run_id,
            attempt=args.attempt,
        )
    ),
    "gcp-smoke": lambda args: run_smoke_command(_deploy_options(args)),
    "gcp-destroy": lambda args: run_destroy(
        DestroyOptions(
            source=args.store or args.inputs,
            confirm=args.confirm,
            environment=args.environment,
            release_manifest=args.release_manifest,
            identity_mode=args.identity,
            name_prefix=args.name_prefix,
            destroy_service_account=args.destroy_service_account,
            lock=args.lock,
            break_lock=args.break_lock,
            trusted_repository_id=args.trusted_repository_id,
            trusted_owner_id=args.trusted_owner_id,
        )
    ),
    "gcp-config": _handle_config,
}


def dispatch_lifecycle(args: argparse.Namespace) -> None:
    """Run one lifecycle command and exit with its status, never with a traceback."""
    from gcp_deploy import GcpDeployError
    from gcp_deploy_stages import DeployStageError
    from gcp_inputs import DeploymentInputError
    from gcp_lock import DeployLockHeldError
    from gcp_process import GcpCommandError

    handled = (
        GcpDeployError,
        DeployStageError,
        DeploymentInputError,
        DeployLockHeldError,
        GcpCommandError,
        RuntimeError,
        ValueError,
    )
    try:
        status = _HANDLERS[args.command](args)
    except handled as exc:
        status = report_failure(exc)
    sys.exit(status)
