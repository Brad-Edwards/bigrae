"""Running Terraform commands, and the ``apply`` / ``destroy`` / ``cleanup_state`` entry points."""

import contextlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from log_redact import safe_log_value

from ._backend import _AWS_BACKEND, get_backend_config
from ._workspace import (
    _TF_INPUT_FALSE,
    _finalize_workspace,
    _run_init_in_staged_workspace,
    _stage_workspace,
    _write_tfvars,
)

logger = logging.getLogger(__name__)

# Bound for the one sanitized stderr copy of a failed command that ADR-043-R5
# allows in the provisioner log.
_STDERR_LOG_MAX_CHARS = 4000


class TerraformCommandError(RuntimeError):
    """A Terraform command exited with a non-zero status.

    The message is authored: it names the subcommand and the exit code only.
    ``run_terraform`` logs Terraform's stderr and keeps it out of the message, so
    the message can go into range error text and operation results (ADR-043-R5).
    """


def run_terraform(
    args: list[str],
    working_dir: Path,
    env: dict[str, str] | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Terraform command.

    Args:
        args: Terraform command arguments (without 'terraform' prefix)
        working_dir: Directory to run command in
        env: Environment variables (merged with current env)
        capture_output: Whether to capture stdout/stderr

    Returns:
        Completed process result

    Raises:
        TerraformCommandError: If command fails
    """
    cmd = ["terraform", *args]
    run_env = {**os.environ, **(env or {})}

    logger.debug("Running: %s in %s", " ".join(cmd), working_dir)

    result = subprocess.run(  # noqa: S603  # NOSONAR — hardcoded binary, list args, no shell
        cmd,
        cwd=working_dir,
        env=run_env,
        capture_output=capture_output,
        text=True,
    )

    if result.returncode != 0:
        subcommand = args[0] if args else "command"
        logger.error(
            "Terraform %s failed exit_code=%d stderr=%s",
            subcommand,
            result.returncode,
            safe_log_value(result.stderr, max_len=_STDERR_LOG_MAX_CHARS),
        )
        # ADR-043-R5 allows only this bounded, sanitized stderr copy, and only in
        # the provisioner log. It stays out of the exception, whose text reaches
        # range error text and operation results.
        raise TerraformCommandError(f"Terraform {subcommand} failed (exit_code={result.returncode})")

    return result


def apply(
    state_key_prefix: str,
    request_uuid: str,
    variables: dict[str, Any],
    working_dir: Path,
    label: str,
) -> dict[str, Any]:
    """Run terraform apply and return outputs.

    Stages a writable per-request workspace, runs init/apply/output from there, and
    removes the staged tree before returning so `terraform.tfvars.json` (which can
    carry secrets) does not persist on the workspace volume.
    """
    staged = _stage_workspace(working_dir, request_uuid, label)
    try:
        _run_init_in_staged_workspace(state_key_prefix, request_uuid, staged, label)

        tfvars_path = _write_tfvars(staged, variables)

        # Late-bound call to ``terraform_base.run_terraform`` so a test patch
        # applied at the package level (``@patch("terraform_base.run_terraform")``)
        # takes effect here — ``apply()`` shares this module with ``run_terraform``'s
        # definition, so a bare call would bypass a patch applied at the package.
        import terraform_base as _tb

        logger.info("Running terraform apply for %s...", label)
        result = _tb.run_terraform(
            [
                "apply",
                "-auto-approve",
                _TF_INPUT_FALSE,
                "-no-color",
                f"-var-file={tfvars_path}",
            ],
            staged,
        )
        # Apply stdout echoes the plan and every non-sensitive output (for
        # example presigned agent URLs), so only its size is logged.
        logger.info("Terraform apply for %s completed (%d bytes of output)", label, len(result.stdout or ""))

        logger.info("Retrieving Terraform outputs...")
        output_result = _tb.run_terraform(
            ["output", "-json", "-no-color"],
            staged,
        )

        try:
            raw_outputs = json.loads(output_result.stdout)
        except json.JSONDecodeError as e:
            # Raw output values can be secrets; the caller logs the translated error.
            raise RuntimeError(f"Failed to parse Terraform output as JSON: {e}") from e

        outputs: dict[str, Any] = {}
        for key, val in raw_outputs.items():
            if not isinstance(val, dict) or "value" not in val:
                logger.warning("Unexpected Terraform output format for key=%s", key)
                continue
            outputs[key] = val["value"]

        # Output values can carry secrets (secret ARNs, presigned URLs): log the keys only.
        logger.info("Retrieved %d Terraform outputs for %s: keys=%s", len(outputs), label, sorted(outputs))
        return outputs
    finally:
        _finalize_workspace(staged)


def destroy(
    state_key_prefix: str,
    request_uuid: str,
    working_dir: Path,
    label: str,
    variables: dict[str, Any] | None = None,
) -> None:
    """Run terraform destroy.

    Stages a writable per-request workspace, runs init/destroy from there, and
    removes the staged tree before returning so `terraform.tfvars.json` does not
    persist on the workspace volume.
    """
    staged = _stage_workspace(working_dir, request_uuid, label)
    try:
        _run_init_in_staged_workspace(state_key_prefix, request_uuid, staged, label)

        logger.info("Running terraform destroy for %s...", label)
        destroy_args = [
            "destroy",
            "-auto-approve",
            _TF_INPUT_FALSE,
            "-no-color",
        ]

        if variables:
            tfvars_path = _write_tfvars(staged, variables)
            destroy_args.append(f"-var-file={tfvars_path}")

        # Late-bound call to ``terraform_base.run_terraform`` so a test patch
        # applied at the package level takes effect here too.
        import terraform_base as _tb

        result = _tb.run_terraform(destroy_args, staged)
        # Destroy stdout echoes the plan, including prior output values, so only its size is logged.
        logger.info("%s Terraform destroy completed successfully (%d bytes of output)", label, len(result.stdout or ""))
    finally:
        _finalize_workspace(staged)


def cleanup_state(state_key_prefix: str, request_uuid: str, label: str) -> None:
    """Delete the backend state file after destroy.

    This removes the state file after resources are destroyed,
    similar to `pulumi stack rm`.

    Args:
        state_key_prefix: Prefix for the state key
        request_uuid: UUID of the provisioning request
        label: Label for log messages
    """
    backend = get_backend_config(state_key_prefix, request_uuid)

    logger.info(
        "Deleting %s Terraform state: %s://%s/%s",
        label,
        backend.backend_type,
        backend.bucket,
        backend.state_object_key,
    )

    # Late-bound call to ``terraform_base.get_object_storage`` so a test patch
    # applied at the package level (``@patch("terraform_base.get_object_storage")``)
    # takes effect here.
    import terraform_base as _tb

    storage = _tb.get_object_storage()

    try:
        storage.delete_object(bucket=backend.bucket, key=backend.state_object_key)
    except Exception as e:  # noqa: BLE001 - best-effort state cleanup after a confirmed destroy; adapter logs traceback
        logger.warning("Failed to delete state file: %s", e)

    if backend.backend_type == _AWS_BACKEND:
        lock_key = f"{backend.state_object_key}.tflock"
        # Best-effort: the lock object is usually absent, and the storage adapter
        # logs any provider failure with its traceback before raising.
        with contextlib.suppress(Exception):
            storage.delete_object(bucket=backend.bucket, key=lock_key)

    logger.info("%s Terraform state cleanup completed", label)
