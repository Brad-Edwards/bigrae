"""Pytest configuration for bootstrap tests."""

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Get the real repo root to protect it
_REAL_REPO_ROOT = Path(__file__).parent.parent.parent.parent.resolve()


@pytest.fixture(autouse=True)
def protect_real_files(monkeypatch):
    """SAFETY: Prevent tests from writing to real repository files.

    This autouse fixture intercepts Path.write_text() calls and raises
    an error if any test tries to write to the real repository.
    """
    original_write_text = Path.write_text

    def safe_write_text(self, *args, **kwargs):
        resolved = self.resolve()
        # Block writes to the real repo (but allow writes to /tmp, etc.)
        if str(resolved).startswith(str(_REAL_REPO_ROOT)):
            raise RuntimeError(
                f"TEST SAFETY: Blocked write to real repo file: {resolved}\n"
                f"Tests must mock get_repo_root() or use tmp_path fixtures."
            )
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", safe_write_text)


@pytest.fixture
def github_identity():
    """Return a synthetic resolved GitHub repository identity (ADR-069)."""
    return {
        "github_org": "example-org",
        "github_repo": "example-repo",
        "github_repository_id": "123456789",
        "github_owner_id": "12345678",
    }


@pytest.fixture
def gh_identity_responder(github_identity):
    """Return a function that answers the gh identity commands, or None for other commands."""
    full_name = f"{github_identity['github_org']}/{github_identity['github_repo']}"
    api_lines = (
        github_identity["github_repository_id"],
        github_identity["github_owner_id"],
        full_name,
        github_identity["github_org"],
    )

    def respond(cmd):
        if list(cmd) == ["gh", "repo", "set-default", "--view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{full_name}\n", stderr="")
        if list(cmd[:3]) == ["gh", "api", f"repos/{full_name}"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(api_lines) + "\n", stderr="")
        return None

    return respond


@pytest.fixture
def gh_default_repository(gh_identity_responder):
    """Answer only the gh identity commands at the subprocess boundary.

    Any other command fails, so a test can not run a real command. The patch
    is a context manager, so it nests inside other subprocess patches.
    """

    def run(cmd, *_args, **_kwargs):
        result = gh_identity_responder(cmd)
        if result is None:
            raise RuntimeError(f"Test attempted to run {cmd} without a fake")
        return result

    with patch("subprocess.run", side_effect=run):
        yield


@contextmanager
def _isolated_runner_imports(mock, monkeypatch):
    """Runner imports made under a mock must not survive into real process tests."""
    names = ("runner", "gcp_runner")
    saved = {name: sys.modules.get(name) for name in names}
    for name in names:
        sys.modules.pop(name, None)
    try:
        with monkeypatch.context() as scope:
            scope.setitem(sys.modules, "deploy", mock)
            scope.setitem(sys.modules, "bootstrap_core", mock)
            yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture
def mock_deploy(monkeypatch):
    """Mock the deploy module for runner.py tests.

    This fixture creates a mock deploy module and patches it into sys.modules
    before the test runs, then cleans up after.
    """
    mock = MagicMock()
    mock.Colors = MagicMock()
    mock.Colors.BOLD = ""
    mock.Colors.END = ""
    mock.Colors.GREEN = ""
    mock.Colors.YELLOW = ""
    mock.Colors.CYAN = ""
    mock.Colors.RED = ""
    mock.code_block = MagicMock()
    mock.confirm = MagicMock(return_value=True)
    mock.confirm_or_manual = MagicMock(return_value="yes")
    mock.error = MagicMock()
    mock.header = MagicMock()
    mock.info = MagicMock()
    mock.run_cmd = MagicMock()
    mock.subheader = MagicMock()
    mock.success = MagicMock()
    mock.wait_for_user = MagicMock()
    mock.warn = MagicMock()

    with _isolated_runner_imports(mock, monkeypatch):
        yield mock


@pytest.fixture
def mock_gcp_deploy(monkeypatch):
    """Mock bootstrap_core for gcp_runner.py tests (issue #1546).

    gcp_runner imports the shared bootstrap_core helpers (including the
    secret-stdin ``run_cmd_secret_stdin`` and the cloud-agnostic
    ``mint_registration_token`` reused from runner.py). Mock the boundary
    (bootstrap_core) rather than first-party gcp_runner seams (ADR-019) and
    clean up the freshly imported modules afterward.
    """
    mock = MagicMock()
    for attr in ("BOLD", "END", "GREEN", "YELLOW", "CYAN", "RED", "BLUE"):
        setattr(mock.Colors, attr, "")
    mock.code_block = MagicMock()
    mock.confirm = MagicMock(return_value=True)
    mock.error = MagicMock()
    mock.header = MagicMock()
    mock.info = MagicMock()
    mock.run_cmd = MagicMock()
    mock.run_cmd_secret_stdin = MagicMock(return_value=0)
    mock.subheader = MagicMock()
    mock.success = MagicMock()
    mock.warn = MagicMock()

    with _isolated_runner_imports(mock, monkeypatch):
        yield mock
