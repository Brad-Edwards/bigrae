#!/usr/bin/env python3
"""Render the GitHub-less deployment input bundle from CI secrets and variables (#2571).

The GCP CI workflows call the same commands as an operator (``deploy.py gcp-deploy``,
``gcp-release-scan``, ``gcp-smoke``, ``gcp-destroy``). This adapter is the only CI
step that knows the GitHub input names. It writes one private bundle directory:

- ``bigrae.yaml``: ``SHIFTER_CONFIG_GCP_DEV`` with the GitHub variables projected
  onto their settings homes (ADR-070 "Input homes");
- ``terraform.tfvars``: the committed per-deployment Terraform values;
- ``terraform.tfvars.json``: the Terraform values that GitHub secrets hold;
- ``range-images.json``, ``foundation-inputs.json``, and the committed broker template
  and retired-artifact inventory, when they exist.

A GitHub value that conflicts with a value in the root config fails the render, so
each input keeps one meaning. ``--purpose destroy`` renders the teardown inputs: a
teardown seeds no operator, so it needs no operator inputs. Secret values stay in the 0600 files of a 0700
directory that the workflow removes at job end. The adapter prints no values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from apply_shifter_config_overlay import ConfigOverlayError, apply_overlay

REPO_ROOT = Path(__file__).resolve().parents[2]
PURPOSES = ("deploy", "destroy")
_ENVIRONMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RANGE_IMAGE_VARIABLES = {
    "linux": "GCP_RANGE_LINUX_IMAGE",
    "kali": "GCP_RANGE_KALI_IMAGE",
    "windows": "GCP_RANGE_WINDOWS_IMAGE",
    "dc": "GCP_RANGE_DC_IMAGE",
}


class CiInputsError(ValueError):
    """The CI inputs can not form a valid deployment input bundle."""


def _text(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _required(env: Mapping[str, str], name: str) -> str:
    value = _text(env, name)
    if not value:
        raise CiInputsError(f"required CI input {name} is not set; see docs/dev/deploy-secrets.md")
    return value


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _boolean(name: str, value: str) -> bool | None:
    if not value:
        return None
    if value.lower() not in {"true", "false"}:
        raise CiInputsError(f"CI input {name} must be true or false")
    return value.lower() == "true"


def _integer(name: str, value: str) -> int | None:
    if not value:
        return None
    if not value.isdigit():
        raise CiInputsError(f"CI input {name} must be a non-negative integer")
    return int(value)


def _json_object(name: str, value: str) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise CiInputsError(f"CI input {name} must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise CiInputsError(f"CI input {name} must be a JSON object")
    return parsed


def _empty(value: object) -> bool:
    return value is None or value == "" or value == [] or value == {}


def project_value(config: dict[str, Any], path: tuple[str, ...], value: object, source: str) -> None:
    """Set ``config[path]`` from a CI input, or fail when the root config disagrees."""
    if _empty(value):
        return
    node = config
    for key in path[:-1]:
        child = node.setdefault(key, {})
        if not isinstance(child, dict):
            raise CiInputsError(f"root config {'.'.join(path[:-1])} must be a mapping")
        node = child
    current = node.get(path[-1])
    if not _empty(current) and current != value:
        raise CiInputsError(f"{source} conflicts with {'.'.join(path)} in the root config; keep the value in one place")
    node[path[-1]] = value


def _projections(env: Mapping[str, str]) -> list[tuple[tuple[str, ...], object, str]]:
    """Return every GitHub input with its root-config home, in a fixed order."""
    region = _text(env, "GCP_REGION")
    skip_operator = _text(env, "SHIFTER_SKIP_OPERATOR_BOOTSTRAP").lower() == "true"
    return [
        (("settings", "project_id"), _required(env, "GCP_PROJECT_ID"), "GCP_PROJECT_ID"),
        (("settings", "region"), region, "GCP_REGION"),
        (("deployment", "domain"), _required(env, "GCP_PUBLIC_HOSTNAME"), "GCP_PUBLIC_HOSTNAME"),
        (("settings", "range_cells", "backend"), _text(env, "GCP_RANGE_BACKEND"), "GCP_RANGE_BACKEND"),
        (("settings", "range_cells", "zone"), _text(env, "RANGE_NETWORK_ZONE"), "RANGE_NETWORK_ZONE"),
        (("settings", "range_cells", "zones"), _csv(_text(env, "RANGE_NETWORK_ZONES")), "RANGE_NETWORK_ZONES"),
        (
            ("settings", "range_cells", "project_id"),
            _text(env, "GCP_RANGE_CELL_PROJECT_ID"),
            "GCP_RANGE_CELL_PROJECT_ID",
        ),
        (
            ("settings", "range_cells", "host_identity_pool_size"),
            _integer("GCP_RANGE_HOST_IDENTITY_POOL_SIZE", _text(env, "GCP_RANGE_HOST_IDENTITY_POOL_SIZE")),
            "GCP_RANGE_HOST_IDENTITY_POOL_SIZE",
        ),
        (
            ("settings", "range_cells", "private_google_access"),
            _boolean("GCP_RANGE_PRIVATE_GOOGLE_ACCESS", _text(env, "GCP_RANGE_PRIVATE_GOOGLE_ACCESS")),
            "GCP_RANGE_PRIVATE_GOOGLE_ACCESS",
        ),
        (
            ("settings", "range_cells", "image_key_profiles"),
            _json_object("GCP_RANGE_IMAGE_KEY_PROFILES_JSON", _text(env, "GCP_RANGE_IMAGE_KEY_PROFILES_JSON")),
            "GCP_RANGE_IMAGE_KEY_PROFILES_JSON",
        ),
        (
            ("settings", "participant_openvpn", "enabled"),
            _boolean("RANGE_OPENVPN_ENABLED", _text(env, "RANGE_OPENVPN_ENABLED")),
            "RANGE_OPENVPN_ENABLED",
        ),
        (("settings", "ctf_content_bucket"), _text(env, "GCP_CTF_CONTENT_BUCKET"), "GCP_CTF_CONTENT_BUCKET"),
        (
            ("settings", "operator_bootstrap", "enabled"),
            False if skip_operator else None,
            "SHIFTER_SKIP_OPERATOR_BOOTSTRAP",
        ),
        (
            ("settings", "operator_bootstrap", "admin_email"),
            _text(env, "GCP_BOOTSTRAP_ADMIN_EMAIL"),
            "GCP_BOOTSTRAP_ADMIN_EMAIL",
        ),
        (
            ("settings", "operator_bootstrap", "staff_emails"),
            _csv(_text(env, "PLATFORM_BOOTSTRAP_STAFF_EMAILS")),
            "PLATFORM_BOOTSTRAP_STAFF_EMAILS",
        ),
        (
            ("settings", "operator_bootstrap", "superuser_emails"),
            _csv(_text(env, "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS")),
            "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS",
        ),
        (
            ("settings", "post_deploy_smoke", "user_email"),
            _text(env, "SMOKE_TEST_USER_EMAIL"),
            "SMOKE_TEST_USER_EMAIL",
        ),
    ]


def render_config(raw_config: str, env: Mapping[str, str], *, purpose: str = "deploy") -> dict[str, Any]:
    """Return the root config with every GitHub input projected onto its home."""
    try:
        config = yaml.safe_load(raw_config)
    except yaml.YAMLError:
        raise CiInputsError("SHIFTER_CONFIG_GCP_DEV must be valid YAML") from None
    if not isinstance(config, dict) or not isinstance(config.get("settings"), dict):
        raise CiInputsError("SHIFTER_CONFIG_GCP_DEV must be a root config with a settings mapping")
    for path, value, source in _projections(env):
        project_value(config, path, value, source)
    settings = config["settings"]
    if _empty(settings.get("region")):
        settings["region"] = "us-central1"
    if purpose == "destroy" and _empty(settings.get("operator_bootstrap")):
        settings["operator_bootstrap"] = {"enabled": False}
    # The CI deploy always seeds the first operator unless the audited opt-out is
    # set, so the settings validation requires the operator email (as before).
    settings.setdefault("operator_bootstrap", {})
    return config


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _copy_private(source: Path, target: Path) -> None:
    with open(source, encoding="utf-8") as handle:
        _write_private(target, handle.read())


def _validate(config_path: Path) -> None:
    from installation.errors import InstallationConfigError
    from installation.loader import load_root_config
    from installation.settings_gcp import GcpBackendSettings

    try:
        root = load_root_config(config_path)
        GcpBackendSettings.from_root_settings(root.settings)
    except (InstallationConfigError, ValueError) as exc:
        raise CiInputsError(f"the rendered root config is invalid: {exc}") from None


def render_bundle(
    environment: str,
    output: Path,
    env: Mapping[str, str],
    *,
    purpose: str = "deploy",
    repo_root: Path | None = None,
    validate: Callable[[Path], None] = _validate,
) -> list[str]:
    """Write the bundle for ``environment`` into the new private directory ``output``."""
    repo_root = repo_root or REPO_ROOT
    if not _ENVIRONMENT_RE.fullmatch(environment):
        raise CiInputsError("--environment must be a lowercase DNS label")
    committed_values = repo_root / "platform/terraform/gcp/environments" / environment / "terraform.tfvars"
    if not committed_values.is_file():
        raise CiInputsError(f"no committed Terraform values for {environment}")
    deploy_dir = repo_root / "platform/deploy/gcp" / environment
    if purpose not in PURPOSES:
        raise CiInputsError("--purpose must be deploy or destroy")
    config = render_config(_required(env, "SHIFTER_CONFIG_GCP_DEV"), env, purpose=purpose)
    secret_values = {
        "identity_allowed_email_domain": _required(env, "GCP_IDENTITY_ALLOWED_EMAIL_DOMAIN"),
        # The private GKE endpoint is reached through Connect Gateway.
        "gke_master_authorized_cidrs": [],
    }
    range_images = {role: _text(env, name) for role, name in _RANGE_IMAGE_VARIABLES.items() if _text(env, name)}
    foundation = _json_object("GCP_FOUNDATION_INPUTS_JSON", _text(env, "GCP_FOUNDATION_INPUTS_JSON"))

    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    try:
        written = _write_bundle(output, config, committed_values, deploy_dir, secret_values, env)
        if range_images:
            _write_private(output / "range-images.json", json.dumps(range_images, sort_keys=True) + "\n")
            written.append("range-images.json")
        if foundation is not None:
            _write_private(output / "foundation-inputs.json", json.dumps(foundation, sort_keys=True) + "\n")
            written.append("foundation-inputs.json")
        validate(output / "bigrae.yaml")
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return written


def _write_bundle(
    output: Path,
    config: dict[str, Any],
    committed_values: Path,
    deploy_dir: Path,
    secret_values: dict[str, object],
    env: Mapping[str, str],
) -> list[str]:
    written = ["bigrae.yaml", "terraform.tfvars", "terraform.tfvars.json"]
    config_path = output / "bigrae.yaml"
    _write_private(config_path, yaml.safe_dump(config, sort_keys=False))
    _copy_private(committed_values, output / "terraform.tfvars")
    _write_private(output / "terraform.tfvars.json", json.dumps(secret_values, sort_keys=True) + "\n")
    template = deploy_dir / "model-broker-overlay.template.json"
    if template.is_file():
        _copy_private(template, output / template.name)
        written.append(template.name)
    else:
        try:
            apply_overlay(config_path, _text(env, "SHIFTER_CONFIG_OVERLAY_JSON"))
        except ConfigOverlayError as exc:
            raise CiInputsError(f"SHIFTER_CONFIG_OVERLAY_JSON is invalid: {exc}") from None
    inventory = deploy_dir / "retired-cluster-artifacts.json"
    if inventory.is_file():
        _copy_private(inventory, output / inventory.name)
        written.append(inventory.name)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--environment", required=True, help="CI deployment ID (the workflow environment)")
    parser.add_argument("--output", required=True, type=Path, help="New private bundle directory")
    parser.add_argument("--purpose", choices=PURPOSES, default="deploy", help="deploy (default) or destroy")
    args = parser.parse_args(argv)
    try:
        written = render_bundle(args.environment, args.output, os.environ, purpose=args.purpose)
    except (CiInputsError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"Rendered the {args.environment} deployment inputs: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
