"""Isolated Kubernetes access for the GCP deploy commands (#2571).

The private GKE control plane is reached only through the IAM-authenticated fleet
Connect Gateway. Each run writes its credentials to a private ``KUBECONFIG`` in its
working directory, so a deploy never changes the operator's default kubeconfig context.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from gcp_inputs import private_workdir
from gcp_process import ProcessRunner


def cluster_name(environment: str) -> str:
    """Return the deterministic platform cluster and fleet membership name."""
    return f"shifter-{environment}-gke"


def kube_env(
    runner: ProcessRunner,
    identity_env: Mapping[str, str | None],
    *,
    workdir: Path,
    project_id: str,
    location: str,
    cluster: str,
) -> dict[str, str | None]:
    """Fetch Connect Gateway credentials into a private kubeconfig and return its environment."""
    directory = private_workdir(workdir / "kube")
    kubeconfig = directory / "config"
    env: dict[str, str | None] = {
        **identity_env,
        "KUBECONFIG": str(kubeconfig),
        "USE_GKE_GCLOUD_AUTH_PLUGIN": "True",
    }
    runner.run(
        [
            "gcloud",
            "container",
            "fleet",
            "memberships",
            "get-credentials",
            cluster,
            "--location",
            location,
            "--project",
            project_id,
        ],
        env=env,
    )
    if kubeconfig.exists():
        kubeconfig.chmod(0o600)
    return env


def kubectl_json(runner: ProcessRunner, env: Mapping[str, str | None], *args: str) -> dict[str, object]:
    """Run ``kubectl ... -o json`` and return the parsed object."""
    result = runner.run(["kubectl", *args, "-o", "json"], env=env)
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def apply_manifest(runner: ProcessRunner, env: Mapping[str, str | None], manifest: Mapping[str, object]) -> None:
    """Apply one manifest from stdin, so its content never reaches argv."""
    runner.run(["kubectl", "apply", "-f", "-"], env=env, input_text=json.dumps(manifest), sensitive=True)
