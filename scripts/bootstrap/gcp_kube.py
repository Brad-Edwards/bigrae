"""Isolated Kubernetes access for the GCP deploy commands (#2571).

The private GKE control plane is reached only through the IAM-authenticated fleet
Connect Gateway. Each run writes its credentials to a private ``KUBECONFIG`` in its
working directory, so a deploy never changes the operator's default kubeconfig context.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from bootstrap_core import warn
from gcp_inputs import private_workdir
from gcp_process import GcpCommandError, ProcessRunner, diagnostic_tail

_LOG_READ_ATTEMPTS = 5
_LOG_READ_DELAY_SECONDS = 15


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


def read_job_log(
    runner: ProcessRunner,
    job: str,
    *,
    namespace: str,
    container: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return the log of a completed Job, retrying the idempotent read (#2687).

    A new private cluster can briefly fail to proxy a log request to the node.
    A failure reports kubectl's own stderr only: the container output on stdout
    can carry private values.
    """
    argv = ["kubectl", "logs", f"job/{job}", "-n", namespace, *(["-c", container] if container else [])]
    for attempt in range(1, _LOG_READ_ATTEMPTS + 1):
        result = runner.run(argv, check=False, sensitive=True)
        if result.returncode == 0:
            return result.stdout
        detail = diagnostic_tail(result.stderr) or "kubectl wrote no error"
        if attempt < _LOG_READ_ATTEMPTS:
            warn(
                f"Reading the job/{job} log failed: {detail}; "
                f"retrying in {_LOG_READ_DELAY_SECONDS}s ({attempt}/{_LOG_READ_ATTEMPTS})"
            )
            sleep(_LOG_READ_DELAY_SECONDS)
    raise GcpCommandError(f"kubectl logs failed with exit status {result.returncode}: {detail}")


def apply_manifest(runner: ProcessRunner, env: Mapping[str, str | None], manifest: Mapping[str, object]) -> None:
    """Apply one manifest from stdin, so its content never reaches argv."""
    runner.run(["kubectl", "apply", "-f", "-"], env=env, input_text=json.dumps(manifest), sensitive=True)
