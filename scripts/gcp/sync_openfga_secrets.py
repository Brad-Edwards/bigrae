#!/usr/bin/env python3
"""Deliver named policy bundles through provider-native and Kubernetes Secrets."""

import argparse
import json
import subprocess  # nosec B404
from pathlib import Path

from installation.loader import load_root_config
from installation.openfga_bundles import policy_documents
from installation.settings_gcp import GcpBackendSettings, GcpOpenFgaSettings


def fetch_secret(reference: str) -> dict:
    _, project, _, secret = reference.split("/")
    # Fixed operator/CI CLI; named references are validated and no shell interprets argv.
    result = subprocess.run(  # nosec B603 B607
        ["gcloud", "secrets", "versions", "access", "latest", "--project", project, "--secret", secret],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Named policy bundle could not be read")
    return json.loads(result.stdout)


def apply_resource(document: dict) -> None:
    # Fixed operator/CI CLI; named references are validated and no shell interprets argv.
    result = subprocess.run(  # nosec B603 B607
        ["kubectl", "apply", "--server-side", "--field-manager=shifter-policy-secrets", "-f", "-"],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Policy credential publication failed")


def sync(settings: object) -> None:
    policy = GcpOpenFgaSettings.model_validate(settings)
    if not policy.enabled:
        return
    runtime = fetch_secret(policy.runtime_secret_ref)
    admin = fetch_secret(policy.admin_secret_ref)
    ca = fetch_secret(policy.ca_secret_ref)
    for document in policy_documents(runtime, admin, ca):
        apply_resource(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = GcpBackendSettings.from_root_settings(load_root_config(args.config).settings)
    sync(settings.openfga.model_dump())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
