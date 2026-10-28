"""Participant OpenVPN pool PKI and rollout for GCP deployments (#2480, #2571).

The tenant CA and the pool server identity live only in Secret Manager. The pool
servers run only the image that the release record names, and only the deploy command
writes the record, after the exact-digest scan and the provenance check. A new record or
a new server identity replaces every server, surging new servers in before old ones
leave.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from bootstrap_core import get_repo_root, info, success
from gcp_inputs import private_workdir
from gcp_process import GcpCommandError, ProcessRunner, script_argv

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class OpenVpnError(RuntimeError):
    """The OpenVPN pool PKI or rollout failed."""


def pool_output(outputs: Mapping[str, Mapping[str, object]]) -> dict[str, str] | None:
    """Return the pool wiring from Terraform, or None when the pool is not deployed."""
    value = outputs.get("openvpn_pool", {}).get("value")
    if not value:
        return None
    if not isinstance(value, Mapping):
        raise OpenVpnError("Terraform output openvpn_pool must be a map")
    return {str(key): str(item) for key, item in value.items()}


def ensure_pki(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    pool: Mapping[str, str],
    project_id: str,
    workdir: Path,
) -> bool:
    """Create or renew the pool PKI. Return True when it changed."""
    directory = private_workdir(workdir / "openvpn")
    changed_file = directory / "pki-changed"
    changed_file.unlink(missing_ok=True)
    runner.run(
        script_argv(
            get_repo_root(),
            "ensure_vpn_pki",
            "--project",
            project_id,
            "--issuer-secret",
            pool["issuer_secret"].rsplit("/", 1)[-1],
            "--server-secret",
            pool["server_secret"].rsplit("/", 1)[-1],
            "--changed-output",
            str(changed_file),
        ),
        env=env,
    )
    return "changed=true" in changed_file.read_text(encoding="utf-8") if changed_file.exists() else False


def _latest_release(runner: ProcessRunner, env: Mapping[str, str | None], secret: str, project_id: str) -> str:
    """Return the recorded pool release, or an empty string before the first record."""
    result = runner.run(
        ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret}", f"--project={project_id}"],
        env=env,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if "NOT_FOUND" in result.stderr:
        return ""
    raise OpenVpnError("cannot read the OpenVPN pool release record")


def _healthy(health: str) -> bool:
    """Return True when every backend of the pool reports HEALTHY."""
    try:
        payload = json.loads(health)
    except json.JSONDecodeError:
        return False
    states = [
        entry.get("healthState")
        for item in payload
        if isinstance(item, Mapping)
        for entry in (item.get("status", {}).get("healthStatus") or [])
        if isinstance(entry, Mapping)
    ]
    return bool(states) and all(state == "HEALTHY" for state in states)


def roll_out_pool(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    pool: Mapping[str, str],
    image: str,
    project_id: str,
    pki_changed: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Record the approved pool image, replace servers when needed, and wait for health.

    ``image`` is the exact openvpn release identity (``<image root>@sha256:<digest>``).
    """
    image_root, _, digest = image.rpartition("@")
    if not _DIGEST_RE.fullmatch(digest):
        raise OpenVpnError("the openvpn release image has no exact digest")
    if pool.get("image_root") != image_root:
        raise OpenVpnError("the pool image root does not match the openvpn release image root")
    secret = pool["release_secret"].rsplit("/", 1)[-1]
    group, region, backend = pool["instance_group"], pool["region"], pool["backend_service"]
    release = f"{image_root}@{digest}"
    replace = pki_changed
    if _latest_release(runner, env, secret, project_id) != release:
        runner.run(
            ["gcloud", "secrets", "versions", "add", secret, f"--project={project_id}", "--data-file=-"],
            env=env,
            input_text=release,
        )
        replace = True
    if replace:
        runner.run(
            [
                "gcloud",
                "compute",
                "instance-groups",
                "managed",
                "rolling-action",
                "replace",
                group,
                f"--region={region}",
                f"--project={project_id}",
                "--max-unavailable=0",
            ],
            env=env,
        )
    for condition in ("--version-target-reached", "--stable"):
        runner.run(
            [
                "gcloud",
                "compute",
                "instance-groups",
                "managed",
                "wait-until",
                group,
                condition,
                f"--region={region}",
                f"--project={project_id}",
                "--timeout=1800",
            ],
            env=env,
        )
    for _ in range(40):
        health = runner.run(
            [
                "gcloud",
                "compute",
                "backend-services",
                "get-health",
                backend,
                f"--region={region}",
                f"--project={project_id}",
                "--format=json",
            ],
            env=env,
            check=False,
        )
        if health.returncode == 0 and _healthy(health.stdout):
            break
        sleep(15)
    else:
        raise OpenVpnError("OpenVPN pool servers did not all report healthy within 10 minutes")
    _destroy_superseded_releases(runner, env, secret, project_id)
    success("OpenVPN pool runs the approved release")


def _destroy_superseded_releases(
    runner: ProcessRunner, env: Mapping[str, str | None], secret: str, project_id: str
) -> None:
    """The pool reads only the latest record; destroy the superseded ones."""
    versions = runner.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "list",
            secret,
            f"--project={project_id}",
            "--filter=state:ENABLED",
            "--sort-by=~createTime",
            "--format=value(name)",
        ],
        env=env,
    ).stdout.split()
    for version in versions[1:]:
        try:
            runner.run(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "destroy",
                    version.rsplit("/", 1)[-1],
                    f"--secret={secret}",
                    f"--project={project_id}",
                    "--quiet",
                ],
                env=env,
            )
        except GcpCommandError as exc:
            raise OpenVpnError(f"cannot destroy a superseded pool release record: {exc}") from None
    info("Superseded pool release records destroyed")
