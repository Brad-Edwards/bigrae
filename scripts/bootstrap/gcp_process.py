"""Fixed-argv process execution for the GitHub-less GCP deploy commands (#2571).

Every GCP lifecycle command runs its tools through :class:`ProcessRunner`. The runner
takes an explicit child environment for each call, so a stage can select exactly one
purpose identity (ADR-070). It logs a redacted argv and never logs the child
environment or stdin. Tests replace the runner with a recording fake.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bootstrap_core import _redact_argv_for_log, info

# The number of stderr lines a failure message keeps. Tool output is diagnostic only;
# secret values travel through the environment or stdin, never argv.
_STDERR_TAIL_LINES = 20


class GcpCommandError(RuntimeError):
    """A deploy command step failed with a bounded, value-free diagnostic."""


@dataclass(frozen=True)
class ProcessResult:
    """The exit status and captured streams of one child process."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def merged_env(base: Mapping[str, str], overlay: Mapping[str, str | None] | None) -> dict[str, str]:
    """Overlay ``overlay`` on ``base``; a ``None`` value removes the key."""
    merged = dict(base)
    for key, value in (overlay or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def _tail(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


class ProcessRunner:
    """Run fixed argv commands with an explicit, per-call environment."""

    def __init__(self, base_env: Mapping[str, str] | None = None) -> None:
        # ``None`` reads the live process environment at call time, so a stage scope
        # (:func:`scoped_environment`) selects the identity for every child process.
        self._base_env = None if base_env is None else dict(base_env)

    @property
    def base_env(self) -> dict[str, str]:
        """Return a copy of the environment every call starts from."""
        return dict(os.environ if self._base_env is None else self._base_env)

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str | None] | None = None,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
        timeout: float | None = None,
        sensitive: bool = False,
        cwd: str | None = None,
        inherit_env: bool = True,
    ) -> ProcessResult:
        """Run ``argv`` and return its result.

        ``env`` entries overlay the base environment; a ``None`` value removes the key.
        ``inherit_env=False`` starts from an empty environment, so a renderer sees only
        the inputs it is given. ``sensitive`` suppresses the
        captured streams from the failure message, for commands that read secrets.
        """
        command = [str(part) for part in argv]
        if not command or not command[0]:
            raise GcpCommandError("refusing to run an empty command")
        child_env = merged_env(self.base_env if inherit_env else {}, env)
        info(f"Running: {_redact_argv_for_log(command)}")
        try:
            # An argv list with no shell; the command layer builds every argv.
            completed = subprocess.run(  # nosec B603
                command,
                env=child_env,
                input=input_text,
                capture_output=capture,
                text=True,
                check=False,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError:
            raise GcpCommandError(f"required tool {command[0]!r} is not on PATH") from None
        except subprocess.TimeoutExpired:
            raise GcpCommandError(f"{command[0]} timed out after {timeout} seconds") from None
        result = ProcessResult(completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and result.returncode != 0:
            detail = "" if sensitive else _tail(result.stderr or result.stdout)
            message = f"{command[0]} {command[1] if len(command) > 1 else ''}".strip()
            message = f"{message} failed with exit status {result.returncode}"
            raise GcpCommandError(f"{message}: {detail}" if detail else message)
        return result


def script_argv(repo_root: Path, name: str, *args: str) -> list[str]:
    """Return the argv that runs ``scripts/gcp/<name>.py`` with this interpreter."""
    script = repo_root / "scripts" / "gcp" / f"{name}.py"
    if not script.is_file():
        raise GcpCommandError(f"GCP deploy script {name}.py is missing")
    return [sys.executable, str(script), *args]


def minimal_env(base: Mapping[str, str]) -> dict[str, str | None]:
    """Return the process basics a child interpreter needs, and nothing else."""
    return {key: base[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in base}


@contextmanager
def scoped_environment(overlay: Mapping[str, str | None]) -> Iterator[None]:
    """Apply ``overlay`` to the process environment for one stage, then restore it.

    A ``None`` value removes the key for the scope. The deploy commands are single
    threaded; the scope makes every child process of the stage, including the legacy
    helpers that call ``subprocess`` directly, use the stage identity and kubeconfig.
    """
    previous = {key: os.environ.get(key) for key in overlay}
    try:
        for key, value in overlay.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
