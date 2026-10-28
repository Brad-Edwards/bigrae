"""Tests for bootstrap_core helpers not covered by module-specific suites.

Currently focuses on ``run_cmd_secret_stdin`` (issue #1546): the secret-stdin
subprocess path used to hand a single-use GitHub registration token to an
interactive ``config.sh`` over ``gcloud compute ssh`` without the token ever
reaching argv, the operator log, or a verbatim child-output dump. It also
covers the ADR-069 repository identity resolver.
"""

import subprocess

import pytest


class TestRunCmdSecretStdin:
    """The secret-stdin path never renders the secret or raw child output."""

    def test_secret_stdin_is_never_logged(self, capsys):
        from bootstrap_core import run_cmd_secret_stdin

        # `cat` echoes stdin to stdout, but the helper captures/discards child
        # output, so the token piped over stdin must not reach the operator log.
        rc = run_cmd_secret_stdin(["cat"], secret_stdin="SUPERSECRETTOKEN\n")

        assert rc == 0
        out = capsys.readouterr().out
        assert "SUPERSECRETTOKEN" not in out
        # The non-secret argv is still logged for auditability.
        assert "cat" in out

    def test_secret_present_in_argv_is_rejected(self):
        from bootstrap_core import run_cmd_secret_stdin

        # A caller must never put the token on the command line; the helper
        # fails closed rather than silently leaking it via /proc/<pid>/cmdline.
        with pytest.raises(ValueError):
            run_cmd_secret_stdin(["echo", "REGTOKEN"], secret_stdin="REGTOKEN\n")

    def test_nonzero_exit_is_returned_without_dumping_child_output(self, capsys):
        from bootstrap_core import run_cmd_secret_stdin

        # The child echoes its stdin (the secret) to stderr and fails. run_cmd's
        # verbatim-stderr dump would leak it; this path must suppress it and
        # still surface the exit code so the caller can fail closed.
        rc = run_cmd_secret_stdin(["sh", "-c", "cat 1>&2; exit 3"], secret_stdin="LEAKYTOKEN\n")

        assert rc == 3
        out = capsys.readouterr().out
        assert "LEAKYTOKEN" not in out

    def test_json_value_redacted_but_error_text_preserved(self, capsys):
        import json as _json

        from bootstrap_core import run_cmd_secret_stdin

        values = _json.dumps({"secretReferences": {"database": "supersecretvalue12345"}, "n": "dev"})
        # Child echoes stdin (the values) then emits a helm-like error and fails.
        rc = run_cmd_secret_stdin(
            ["sh", "-c", "cat; echo 'Error: admission webhook denied the request' 1>&2; exit 1"],
            secret_stdin=values,
        )

        assert rc == 1
        out = capsys.readouterr().out
        assert "supersecretvalue12345" not in out  # long value scrubbed
        assert "REDACTED" in out
        assert "admission webhook denied the request" in out  # error preserved

    def test_dry_run_does_not_execute_or_log_secret(self, capsys):
        from bootstrap_core import run_cmd_secret_stdin

        rc = run_cmd_secret_stdin(["cat"], secret_stdin="tok\n", dry_run=True)

        assert rc == 0
        out = capsys.readouterr().out
        assert "DRY-RUN" in out
        assert "tok" not in out


class TestConfirmAssumeYes:
    """Routine prompts proceed via --yes/assume-yes (issue #1639)."""

    @pytest.fixture(autouse=True)
    def _reset_assume_yes(self):
        # Module-level assume-yes state must not leak between tests.
        import bootstrap_core

        bootstrap_core.set_assume_yes(False)
        yield
        bootstrap_core.set_assume_yes(False)

    def test_non_interactive_uses_default_without_assume_yes(self, monkeypatch):
        import bootstrap_core

        monkeypatch.setattr(bootstrap_core.sys.stdin, "isatty", lambda: False)
        # Without assume-yes, a non-TTY confirm() falls back to default_yes: the
        # original auto-abort behavior for prompts that default to "no".
        assert bootstrap_core.confirm("proceed?", default_yes=False) is False
        assert bootstrap_core.confirm("proceed?", default_yes=True) is True

    def test_non_interactive_proceeds_under_assume_yes(self, monkeypatch):
        import bootstrap_core

        monkeypatch.setattr(bootstrap_core.sys.stdin, "isatty", lambda: False)
        bootstrap_core.set_assume_yes(True)
        assert bootstrap_core.assume_yes_enabled() is True
        # --yes makes routine confirm() prompts proceed without a TTY instead of
        # auto-aborting on the default_yes=False fallback.
        assert bootstrap_core.confirm("proceed?", default_yes=False) is True

    def test_interactive_proceeds_without_prompt_under_assume_yes(self, monkeypatch):
        import bootstrap_core

        monkeypatch.setattr(bootstrap_core.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(
            "builtins.input",
            lambda _prompt: pytest.fail("--yes must bypass interactive prompts"),
        )
        bootstrap_core.set_assume_yes(True)

        assert bootstrap_core.confirm("proceed?", default_yes=False) is True

    def test_confirm_or_manual_returns_manual_non_tty_without_assume_yes(self, monkeypatch):
        import bootstrap_core

        monkeypatch.setattr(bootstrap_core.sys.stdin, "isatty", lambda: False)
        # Without --yes, a non-TTY confirm_or_manual falls back to the manual path.
        assert bootstrap_core.confirm_or_manual("automate?") == "manual"

    def test_confirm_or_manual_proceeds_yes_under_assume_yes(self, monkeypatch):
        import bootstrap_core

        monkeypatch.setattr(bootstrap_core.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(
            "builtins.input",
            lambda _prompt: pytest.fail("--yes must bypass interactive prompts"),
        )
        bootstrap_core.set_assume_yes(True)
        # --yes takes the automated 'yes' path so headless bootstrap sets secrets
        # and writes backend configs rather than silently skipping them (#1639).
        assert bootstrap_core.confirm_or_manual("automate?") == "yes"


class TestSubprocessPagerSuppression:
    """run_cmd forces AWS_PAGER="" so aws v2 never blocks on its pager (issue #1639)."""

    def test_subprocess_env_forces_empty_aws_pager(self):
        from bootstrap_core import _subprocess_env

        assert _subprocess_env()["AWS_PAGER"] == ""

    def test_run_cmd_sets_empty_aws_pager_in_child_env(self):
        from bootstrap_core import run_cmd

        # ${AWS_PAGER+set} -> "set" when present (even empty); ${AWS_PAGER-UNSET}
        # -> the value ("") when set, else "UNSET". "set:" proves present-and-empty,
        # distinguishing a forced empty value from an unset variable.
        result = run_cmd(
            ["sh", "-c", 'printf %s "${AWS_PAGER+set}:${AWS_PAGER-UNSET}"'],
            capture=True,
        )

        assert result is not None
        assert result.stdout == "set:"


class TestResolveGithubRepository:
    """ADR-069: the target comes from the gh default and the IDs from the GitHub API."""

    @staticmethod
    def _runner(default_stdout, api_result):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if cmd[:3] == ["gh", "repo", "set-default"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=default_stdout, stderr="")
            return api_result

        return run, calls

    def test_resolves_canonical_identity_from_the_api(self, capsys, github_identity):
        from bootstrap_core import resolve_github_repository

        # The local default still names the old repository. The API returns the canonical name.
        api = subprocess.CompletedProcess(
            [], 0, stdout="123456789\n12345678\nexample-org/example-repo\nexample-org\n", stderr=""
        )
        run, calls = self._runner("example-org/old-name\n", api)

        assert resolve_github_repository(command_runner=run) == github_identity
        assert [cmd for cmd, _ in calls] == [
            ["gh", "repo", "set-default", "--view"],
            ["gh", "api", "repos/example-org/old-name", "--jq", ".id, .owner.id, .full_name, .owner.login"],
        ]
        assert all(kwargs == {"check": False, "capture": True} for _, kwargs in calls)
        assert "example-org/example-repo (repository ID 123456789, owner ID 12345678)" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("default_stdout", "api_result", "message"),
        [
            # gh prints nothing on stdout and exits 0 when no default is set.
            ("", None, "No gh default repository is set"),
            ("not a repository\n", None, "is not in the form <owner>/<repo>"),
            (
                "example-org/example-repo\n",
                subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 404: Not Found"),
                "Cannot read repository example-org/example-repo from the GitHub API: HTTP 404: Not Found",
            ),
            (
                "example-org/example-repo\n",
                subprocess.CompletedProcess([], 0, stdout="123456789\n", stderr=""),
                "Cannot read repository example-org/example-repo from the GitHub API: unexpected response",
            ),
            (
                "example-org/example-repo\n",
                subprocess.CompletedProcess(
                    [], 0, stdout="0\n12345678\nexample-org/example-repo\nexample-org\n", stderr=""
                ),
                "invalid identity",
            ),
        ],
    )
    def test_fails_clearly_without_a_usable_identity(self, default_stdout, api_result, message):
        from bootstrap_core import RepositoryIdentityError, resolve_github_repository

        run, calls = self._runner(default_stdout, api_result)

        with pytest.raises(RepositoryIdentityError, match=message):
            resolve_github_repository(command_runner=run)
        # Without a usable default, the API is not called.
        assert len(calls) == (1 if api_result is None else 2)
