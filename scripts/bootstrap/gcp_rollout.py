"""Post-upgrade rollout checks for GCP deployments (#2571, ADR-037-R8).

After the Helm upgrade the deploy command:

- restarts the control-plane workloads so they hydrate current Secret Manager values;
- waits for every Deployment in the closed release inventory and for every superseded
  pod to terminate;
- records the running image of every workload and container, verifies it against the
  approved digests, and stores the evidence privately under ``deployments/``;
- waits for the public ingress address and the managed certificate.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from bootstrap_core import get_repo_root, info, success, warn
from gcp_evidence import EvidenceRecord, EvidenceRun, store_evidence_set
from gcp_inputs import private_workdir, write_private_file
from gcp_process import GcpCommandError, ProcessRunner, script_argv

PLATFORM_NAMESPACE = "shifter-platform"
RESTART_DEPLOYMENTS = (
    "portal-web",
    "guacd",
    "guacamole-client",
    "worker-cms",
    "worker-engine",
    "worker-mc",
    "ctf-scheduler",
)
CERTIFICATE_NAME = "platform-managed-cert"
INGRESS_NAME = "platform-external"


class RolloutError(RuntimeError):
    """A rollout check failed after the Helm upgrade."""


def _broker_flag(enabled: bool) -> str:
    """Return the command-line text of the model broker flag."""
    return "true" if enabled else "false"


def restart_workloads(runner: ProcessRunner, env: Mapping[str, str | None], *, model_broker_enabled: bool) -> None:
    """Restart the control-plane Deployments that read hydrated secrets at start."""
    deployments = [*RESTART_DEPLOYMENTS, *(["model-access-control"] if model_broker_enabled else [])]
    for deployment in deployments:
        runner.run(["kubectl", "rollout", "restart", f"deployment/{deployment}", "-n", PLATFORM_NAMESPACE], env=env)


def release_deployments(
    runner: ProcessRunner, env: Mapping[str, str | None], *, model_broker_enabled: bool
) -> list[str]:
    """Return the closed release Deployment inventory from the running-image verifier."""
    result = runner.run(
        script_argv(
            get_repo_root(),
            "verify_running_image_ids",
            "--list-deployments",
            "--model-broker-enabled",
            _broker_flag(model_broker_enabled),
        ),
        env=env,
    )
    deployments = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not deployments:
        raise RolloutError("the closed release inventory contains no deployments")
    return deployments


def wait_for_rollouts(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    model_broker_enabled: bool,
    terminating_timeout_seconds: int = 300,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for every release Deployment and for superseded pods to finish terminating."""
    for deployment in release_deployments(runner, env, model_broker_enabled=model_broker_enabled):
        runner.run(
            ["kubectl", "rollout", "status", f"deployment/{deployment}", "-n", PLATFORM_NAMESPACE, "--timeout=15m"],
            env=env,
        )
    deadline = clock() + terminating_timeout_seconds
    while True:
        pods = json.loads(
            runner.run(["kubectl", "get", "pods", "-n", PLATFORM_NAMESPACE, "-o", "json"], env=env).stdout or "{}"
        )
        terminating = [
            item
            for item in pods.get("items", [])
            if isinstance(item, dict) and item.get("metadata", {}).get("deletionTimestamp")
        ]
        if not terminating:
            return
        if clock() >= deadline:
            raise RolloutError("timed out waiting for superseded release pods to terminate")
        sleep(5)


def record_running_images(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    identities: Mapping[str, str],
    model_broker_enabled: bool,
    evidence: EvidenceRun,
    workdir: Path,
) -> EvidenceRecord:
    """Verify every running image against the approved digests and store the evidence."""
    directory = private_workdir(workdir / "running-images")
    pods = runner.run(["kubectl", "get", "pods", "-n", PLATFORM_NAMESPACE, "-o", "json"], env=env).stdout
    pods_file = write_private_file(directory / "running-pods.json", pods)
    evidence_file = directory / "running-image-ids.json"
    evidence_file.unlink(missing_ok=True)
    try:
        runner.run(
            script_argv(
                get_repo_root(),
                "verify_running_image_ids",
                "--model-broker-enabled",
                _broker_flag(model_broker_enabled),
                "--pods-json",
                str(pods_file),
                "--output",
                str(evidence_file),
                "--source-sha",
                evidence.source_revision,
                "--expected-image",
                f"portal={identities['portal']}",
                "--expected-image",
                f"guacd={identities['guacd']}",
                "--expected-image",
                f"guacamole-client={identities['guacamole-client']}",
            ),
            env=env,
        )
    except GcpCommandError as exc:
        raise RolloutError(f"running workloads do not match the approved release: {exc}") from None
    evidence_file.chmod(0o600)
    record = store_evidence_set(
        runner,
        env,
        evidence,
        evidence_class="deployments",
        files=[evidence_file],
        result="passed",
        workdir=directory,
    )
    success("Running workload images match the approved release")
    return record


def wait_for_ingress_address(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    attempts: int = 60,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Wait for the public ingress to receive a load-balancer address."""
    for _ in range(attempts):
        result = runner.run(
            [
                "kubectl",
                "get",
                "ingress",
                INGRESS_NAME,
                "-n",
                PLATFORM_NAMESPACE,
                "-o",
                "jsonpath={.status.loadBalancer.ingress[0].ip}",
            ],
            env=env,
            check=False,
        )
        address = result.stdout.strip() if result.returncode == 0 else ""
        if address:
            info(f"Ingress address: {address}")
            return address
        sleep(10)
    raise RolloutError(f"ingress {INGRESS_NAME} did not receive a load-balancer address within 10 minutes")


def _certificate(runner: ProcessRunner, env: Mapping[str, str | None]) -> tuple[list[str], str]:
    """Return the domains and the status of the managed certificate; both are empty when it is absent."""
    result = runner.run(
        ["kubectl", "get", "managedcertificate", CERTIFICATE_NAME, "-n", PLATFORM_NAMESPACE, "-o", "json"],
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return [], ""
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], ""
    domains = [str(domain) for domain in payload.get("spec", {}).get("domains", [])]
    return domains, str(payload.get("status", {}).get("certificateStatus", ""))


def wait_for_certificate(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    hostname: str,
    require_active: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for the managed certificate to cover ``hostname`` (and be Active when required).

    Returns True when the certificate is Active. In first-install mode, a certificate that
    covers the hostname but is not yet Active returns False with a DNS instruction.
    """
    if not require_active:
        for _ in range(12):
            domains, status = _certificate(runner, env)
            if hostname in domains:
                if status != "Active":
                    warn(
                        f"First-install mode: certificate {CERTIFICATE_NAME} covers {hostname} but is not Active yet. "
                        f"Point DNS for {hostname} at the ingress address, then run the deploy again without "
                        "--first-install."
                    )
                    return False
                return True
            sleep(10)
        raise RolloutError(f"certificate {CERTIFICATE_NAME} did not appear for {hostname} within 2 minutes")
    for _ in range(120):
        domains, status = _certificate(runner, env)
        if hostname in domains and status == "Active":
            success(f"Certificate {CERTIFICATE_NAME} is Active for {hostname}")
            return True
        info(
            f"Waiting for certificate {CERTIFICATE_NAME} to cover {hostname} and become Active ({status or 'pending'})"
        )
        sleep(30)
    raise RolloutError(
        f"certificate {CERTIFICATE_NAME} is not Active for {hostname} after 60 minutes; check that DNS for {hostname} "
        "points at the ingress address"
    )
