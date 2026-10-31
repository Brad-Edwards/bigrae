"""Pre-Terraform GCP project steps for the deploy command (#2571).

These steps prepare a project so that the platform Terraform can apply, on the first
install and on every update:

- initialize the Identity Platform config singleton (Terraform cannot create it);
- create and harden the Terraform state bucket;
- create the Artifact Registry service agent before Terraform grants it CMEK access;
- release a disabled model broker's internal load balancer before Terraform removes
  its address.

Each step is idempotent and runs as the selected purpose identity.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from bootstrap_core import info, success
from gcp_process import GcpCommandError, ProcessRunner

STATE_RETENTION_SECONDS = 30 * 24 * 60 * 60
_IDENTITY_TOOLKIT = "https://identitytoolkit.googleapis.com"
_SERVICE_USAGE = "https://serviceusage.googleapis.com"
_PUBLIC_BINDINGS = (
    ("allUsers", "roles/storage.objectViewer"),
    ("allAuthenticatedUsers", "roles/storage.objectViewer"),
)
_LEGACY_PROJECT_BINDINGS = (
    ("projectOwner", "roles/storage.legacyBucketOwner"),
    ("projectEditor", "roles/storage.legacyBucketOwner"),
    ("projectViewer", "roles/storage.legacyBucketReader"),
)

Opener = Callable[..., object]


class PrerequisiteError(RuntimeError):
    """A pre-Terraform project step failed."""


def access_token(runner: ProcessRunner, env: Mapping[str, str | None]) -> str:
    """Return a short-lived access token for the selected identity."""
    token = runner.run(["gcloud", "auth", "print-access-token"], env=env, sensitive=True).stdout.strip()
    if not token:
        raise PrerequisiteError("the selected identity returned no access token")
    return token


def _google_api(
    url: str, *, token: str, project_id: str, method: str = "GET", body: bytes | None = None, opener: Opener
) -> int:
    """Call one allowed Google API endpoint and return the HTTP status."""
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {
        "identitytoolkit.googleapis.com",
        "serviceusage.googleapis.com",
    }:
        raise PrerequisiteError("refusing to call an unexpected Google API endpoint")
    request = urllib_request.Request(  # noqa: S310 - https scheme and Google API host validated above
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": project_id,
        },
        method=method,
    )
    try:
        with opener(request, timeout=30) as response:  # type: ignore[attr-defined] # nosec B310
            response.read()
            return int(getattr(response, "status", 200))
    except urllib_error.HTTPError as exc:
        return exc.code
    except OSError:
        # URLError and TimeoutError are OSError subclasses.
        raise PrerequisiteError("the Google API endpoint is unreachable") from None


def ensure_identity_platform(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    project_id: str,
    opener: Opener = urllib_request.urlopen,
) -> None:
    """Initialize Identity Platform once; ``initializeAuth`` is not idempotent."""
    runner.run(["gcloud", "services", "enable", "identitytoolkit.googleapis.com", "--project", project_id], env=env)
    token = access_token(runner, env)
    status = _google_api(
        f"{_IDENTITY_TOOLKIT}/admin/v2/projects/{project_id}/config", token=token, project_id=project_id, opener=opener
    )
    if status == 200:
        info("Identity Platform already provisioned")
        return
    info(f"Identity Platform config absent (HTTP {status}); initializing")
    init_status = _google_api(
        f"{_IDENTITY_TOOLKIT}/v2/projects/{project_id}/identityPlatform:initializeAuth",
        token=token,
        project_id=project_id,
        method="POST",
        body=b"{}",
        opener=opener,
    )
    if init_status != 200:
        raise PrerequisiteError(
            f"Identity Platform initializeAuth failed (config HTTP {status}, init HTTP {init_status})"
        )
    success("Identity Platform initialized")


def _bucket_policy(
    runner: ProcessRunner, env: Mapping[str, str | None], bucket: str, project_id: str
) -> dict[str, object]:
    """Return the IAM policy of the state bucket."""
    result = runner.run(
        ["gcloud", "storage", "buckets", "get-iam-policy", f"gs://{bucket}", "--project", project_id, "--format=json"],
        env=env,
    )
    try:
        policy = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise PrerequisiteError("the state bucket IAM policy is not readable") from None
    return policy if isinstance(policy, dict) else {}


def remove_deleted_role_bindings(runner: ProcessRunner, env: Mapping[str, str | None], *, project_id: str) -> None:
    """Remove project IAM bindings to the project's deleted custom roles (#2702).

    Such a binding grants nothing, but GCP rejects each full project policy
    write that keeps it, so no Terraform IAM change could apply. It remains
    when one Terraform root deletes a principal before another root deletes
    the custom role that grants to it.
    """
    listed = runner.run(
        ["gcloud", "iam", "roles", "list", "--project", project_id, "--show-deleted", "--format=json(name,deleted)"],
        env=env,
    ).stdout
    policy_text = ""
    try:
        deleted = {str(role["name"]) for role in json.loads(listed or "[]") if role.get("deleted")}
        if not deleted:
            return
        policy_text = runner.run(["gcloud", "projects", "get-iam-policy", project_id, "--format=json"], env=env).stdout
        bindings = json.loads(policy_text or "{}").get("bindings") or []
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
        raise PrerequisiteError("the project custom roles or IAM policy are not readable") from None
    for binding in bindings:
        role = binding.get("role")
        if role not in deleted:
            continue
        for member in binding.get("members") or []:
            runner.run(
                [
                    "gcloud",
                    "projects",
                    "remove-iam-policy-binding",
                    project_id,
                    f"--member={member}",
                    f"--role={role}",
                    "--all",
                ],
                env=env,
            )
        info(f"Removed the bindings to the deleted custom role {role}")


def ensure_state_bucket(
    runner: ProcessRunner, env: Mapping[str, str | None], *, bucket: str, project_id: str, region: str
) -> None:
    """Create the private, versioned state bucket and remove broad or legacy bindings."""
    exists = runner.run(
        ["gcloud", "storage", "buckets", "describe", f"gs://{bucket}", "--project", project_id], env=env, check=False
    )
    if exists.returncode != 0:
        runner.run(
            [
                "gcloud",
                "storage",
                "buckets",
                "create",
                f"gs://{bucket}",
                f"--project={project_id}",
                f"--location={region}",
                "--uniform-bucket-level-access",
                "--public-access-prevention",
            ],
            env=env,
        )
    runner.run(["gcloud", "storage", "buckets", "update", f"gs://{bucket}", "--versioning"], env=env)
    current = runner.run(
        [
            "gcloud",
            "storage",
            "buckets",
            "describe",
            f"gs://{bucket}",
            "--project",
            project_id,
            "--format=value(retentionPolicy.retentionPeriod)",
        ],
        env=env,
    ).stdout.strip()
    # Keep a stricter existing policy; only raise the retention when it is shorter.
    if not current or int(current) < STATE_RETENTION_SECONDS:
        runner.run(
            [
                "gcloud",
                "storage",
                "buckets",
                "update",
                f"gs://{bucket}",
                "--project",
                project_id,
                f"--retention-period={STATE_RETENTION_SECONDS}s",
            ],
            env=env,
        )
    policy = _bucket_policy(runner, env, bucket, project_id)
    bindings = policy.get("bindings") or []
    unwanted = [
        *_PUBLIC_BINDINGS,
        *((f"{member}:{project_id}", role) for member, role in _LEGACY_PROJECT_BINDINGS),
    ]
    for member, role in unwanted:
        if any(binding.get("role") == role and member in (binding.get("members") or []) for binding in bindings):
            runner.run(
                [
                    "gcloud",
                    "storage",
                    "buckets",
                    "remove-iam-policy-binding",
                    f"gs://{bucket}",
                    f"--project={project_id}",
                    f"--member={member}",
                    f"--role={role}",
                ],
                env=env,
            )
            info(f"Removed {member} from {role} on the state bucket")


def release_disabled_broker_load_balancer(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    broker_enabled: bool,
    project_id: str,
    region: str,
    cluster: str,
    kube_env: Mapping[str, str | None],
) -> None:
    """Delete the broker Service before Terraform releases its reserved address."""
    if broker_enabled:
        return
    membership = runner.run(
        [
            "gcloud",
            "container",
            "fleet",
            "memberships",
            "describe",
            cluster,
            "--location",
            region,
            "--project",
            project_id,
        ],
        env=env,
        check=False,
    )
    if membership.returncode != 0:
        return
    try:
        runner.run(
            [
                "kubectl",
                "delete",
                "service",
                "model-broker",
                "-n",
                "shifter-platform",
                "--ignore-not-found",
                "--wait=true",
                "--timeout=300s",
            ],
            env=kube_env,
        )
    except GcpCommandError as exc:
        raise PrerequisiteError(f"cannot release the disabled model broker load balancer: {exc}") from None
