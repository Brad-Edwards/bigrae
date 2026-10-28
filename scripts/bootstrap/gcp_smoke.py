"""Advisory post-deploy range smoke for GCP deployments (#1638, #2571, ADR-003-R6).

The smoke runs ``scripts/smoke-test-gcp.sh``, the same script CI runs. It launches a
short-lived Job from the live portal template and drives the shared
``run_post_deploy_smoke`` command. The smoke identity comes from
``settings.post_deploy_smoke.user_email``; an empty value skips the smoke. A failure
does not undo the deployment: the deploy command reports it with exit status 3.
"""

from __future__ import annotations

from collections.abc import Mapping

from bootstrap_core import get_repo_root, info, success, warn
from gcp_process import GcpCommandError, ProcessRunner

SMOKE_FAILED_EXIT_STATUS = 3


def run_smoke(
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    user_email: str,
    run_id: str,
    variant: str = "linux",
) -> bool:
    """Run the post-deploy smoke. Return True when it passed or was skipped."""
    if not user_email:
        warn("settings.post_deploy_smoke.user_email is empty; skipping the post-deploy smoke")
        return True
    if variant not in ("linux", "windows"):
        raise ValueError("smoke variant must be linux or windows")
    repo_root = get_repo_root()
    info(f"Running the {variant} post-deploy smoke")
    try:
        runner.run(
            ["bash", str(repo_root / "scripts" / "smoke-test-gcp.sh"), "--variant", variant],
            env={**env, "SMOKE_TEST_USER_EMAIL": user_email, "SMOKE_RUN_ID": run_id},
            capture=False,
        )
    except GcpCommandError:
        warn("The post-deploy smoke failed; the deployment itself completed")
        return False
    success("Post-deploy smoke passed")
    return True
