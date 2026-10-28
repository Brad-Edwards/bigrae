"""Deployment config bucket management (``gcp-config``) for GitHub-less GCP deploys (#2571).

The config bucket holds the deployment input bundle: ``bigrae.yaml``, the Terraform
values, generated range image references, and the optional inputs. Operators manage it
with their own Google identity:

- ``publish`` validates a local bundle and uploads it;
- ``pull`` downloads the bundle into a private directory;
- ``show`` validates the stored bundle and prints a value-free summary;
- ``set-range-image`` records one exact range guest image reference.

The bundle never contains secret values. Secret Manager holds them, and the config
refers to them by reference.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from bootstrap_core import info, success
from gcp_inputs import (
    BUNDLE_OBJECTS,
    RANGE_IMAGE_ROLES,
    RANGE_IMAGES_OBJECT,
    DeploymentInputError,
    fetch_bundle,
    gcs_object_url,
    load_deployment_inputs,
    parse_range_images,
    private_workdir,
    write_private_file,
)
from gcp_process import ProcessRunner


def _require_store(store: str) -> str:
    gcs_object_url(store, "")
    return store


def publish(store: str, source_dir: str, *, runner: ProcessRunner | None = None) -> list[str]:
    """Validate a local bundle and upload every bundle object it contains."""
    runner = runner or ProcessRunner()
    _require_store(store)
    with tempfile.TemporaryDirectory(prefix="bigrae-gcp-config-") as scratch:
        inputs = load_deployment_inputs(source_dir, Path(scratch), runner, {})
        uploaded: list[str] = []
        for name in BUNDLE_OBJECTS:
            local = Path(scratch) / name
            if local.is_file():
                runner.run(["gcloud", "storage", "cp", "--quiet", str(local), gcs_object_url(store, name)])
                uploaded.append(name)
    success(f"Published the {inputs.environment} deployment inputs: {', '.join(uploaded)}")
    return uploaded


def pull(store: str, output_dir: str, *, runner: ProcessRunner | None = None) -> list[str]:
    """Download the stored bundle into a private directory."""
    runner = runner or ProcessRunner()
    _require_store(store)
    target = private_workdir(Path(output_dir))
    copied = fetch_bundle(store, target, runner, {})
    success(f"Pulled {', '.join(sorted(copied))} into {target}")
    return sorted(copied)


def show(store: str, *, runner: ProcessRunner | None = None) -> dict[str, object]:
    """Validate the stored bundle and print a summary without secret material."""
    runner = runner or ProcessRunner()
    _require_store(store)
    workdir = Path(tempfile.mkdtemp(prefix="bigrae-gcp-config-"))
    try:
        inputs = load_deployment_inputs(store, workdir, runner, {})
        settings = inputs.settings
        summary: dict[str, object] = {
            "environment": inputs.environment,
            "project_id": inputs.project_id,
            "region": inputs.region,
            "public_hostname": inputs.public_hostname,
            "profile": inputs.profile,
            "release_trust": settings.release_trust is not None,  # type: ignore[attr-defined]
            "operator_bootstrap": settings.operator_bootstrap is not None,  # type: ignore[attr-defined]
            "range_image_roles": sorted(inputs.range_images),
            "optional_inputs": sorted(
                path.name
                for path in (inputs.foundation_inputs_path, inputs.broker_template_path, inputs.retired_artifacts_path)
                if path is not None
            ),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def write_range_images(
    store: str, images: dict[str, str], *, runner: ProcessRunner | None = None, replace: bool = False
) -> dict[str, str]:
    """Merge ``images`` into ``range-images.json`` in the store and upload it."""
    runner = runner or ProcessRunner()
    _require_store(store)
    unknown = sorted(set(images) - set(RANGE_IMAGE_ROLES))
    if unknown:
        raise DeploymentInputError(f"unsupported range image roles: {', '.join(unknown)}")
    with tempfile.TemporaryDirectory(prefix="bigrae-gcp-config-") as scratch:
        directory = private_workdir(Path(scratch))
        current: dict[str, str] = {}
        if not replace:
            copied = fetch_bundle(store, directory, runner, {})
            current = parse_range_images(copied.get(RANGE_IMAGES_OBJECT))
        merged = {**current, **images}
        local = write_private_file(directory / RANGE_IMAGES_OBJECT, json.dumps(merged, indent=2, sort_keys=True) + "\n")
        parse_range_images(local)
        runner.run(["gcloud", "storage", "cp", "--quiet", str(local), gcs_object_url(store, RANGE_IMAGES_OBJECT)])
    info("Range images: " + ", ".join(f"{role}" for role in sorted(merged)))
    return merged
