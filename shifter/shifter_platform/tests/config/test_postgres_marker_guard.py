"""Regression test for the PostgreSQL-lane fail-closed marker guard (#1524).

The guard lives in the root ``conftest.py``. Its contract: when the run selects
the real PostgreSQL backend (``TEST_DB_BACKEND=postgres``), the run must fail if
NO postgres-marked test actually executes — even when thousands of unmarked
tests pass. The original implementation counted marked items in
``pytest_collection_modifyitems`` *before* pytest's core ``-m`` deselection, so a
``-m "not postgres"`` run counted the marked tests, deselected them all, and
still exited zero. This drives a real subprocess pytest to prove the guard now
keys off tests that actually ran (via ``pytest_runtest_logreport``), across both
the single-process and xdist controller paths.

This test is intentionally unmarked (no DB access): the postgres-marked target
file is deselected, so no database connection is attempted and it runs on the
fast SQLite lane too.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SHIFTER_PLATFORM = Path(__file__).resolve().parents[2]


def _run_pytest_subprocess(extra_args: list[str], *, test_db_backend: str) -> subprocess.CompletedProcess[str]:
    """Run a child pytest that selects only unmarked tests, deselecting postgres ones."""
    # Strip the outer run's coverage/pytest env injections so the child pytest
    # is not steered by the parent's coverage datafile/config.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV", "PYTEST"))}
    env.update(
        {
            "TESTING": "1",
            "TEST_DB_BACKEND": test_db_backend,
            "ENVIRONMENT": "test",
            "DJANGO_SECRET_KEY": "postgres-marker-guard-regression",
        }
    )
    args = [
        sys.executable,
        "-m",
        "pytest",
        # test_env_manifest.py: fast, unmarked, no DB — the "thousands of unmarked
        # tests" stand-in. test_postgres_lane.py: the only postgres-marked file,
        # deselected by `-m "not postgres"`.
        "tests/config/test_env_manifest.py",
        "tests/config/test_postgres_lane.py",
        "-m",
        "not postgres",
        "-p",
        "no:cacheprovider",
        *extra_args,
    ]
    return subprocess.run(env=env, cwd=_SHIFTER_PLATFORM, args=args, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("parallelism", ["-n0", "-n2"])
def test_postgres_lane_fails_when_all_marked_tests_deselected(parallelism):
    """Deselecting every postgres-marked test in the PostgreSQL lane exits nonzero."""
    result = _run_pytest_subprocess([parallelism], test_db_backend="postgres")
    assert result.returncode != 0, (
        "fail-closed guard did not fail the postgres lane when all postgres-marked "
        f"tests were deselected ({parallelism}).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_sqlite_lane_unaffected_when_postgres_tests_deselected():
    """The same selection on the SQLite lane is a normal pass (guard is inert)."""
    result = _run_pytest_subprocess(["-n0"], test_db_backend="sqlite")
    assert result.returncode == 0, (
        "SQLite lane must not be affected by the postgres marker guard.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


# --- Skip-branch coverage ---------------------------------------------------
# The guard has a second failure condition: at least one postgres-marked test is
# skipped while others run. Deleting the skip-tracking line in
# pytest_runtest_logreport (or the skip check in _postgres_guard_failures) would
# not be caught by the deselection scenarios above. This drives the REAL guard
# functions (loaded from the root conftest via importlib) end-to-end in an
# isolated pytest run, so the wiring — logreport records the skip, sessionfinish
# fails closed — is proven. No database is touched: one marked test passes with a
# bare assert and one is skipped, so no live PostgreSQL is required.

_ISOLATED_CONFTEST = f"""
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "_real_root_conftest", {str(_SHIFTER_PLATFORM / "conftest.py")!r}
)
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)

# Re-export only the guard hooks so pytest registers them against the real
# module's state (the hook functions close over the real conftest's globals).
pytest_runtest_logreport = _real.pytest_runtest_logreport
pytest_sessionfinish = _real.pytest_sessionfinish
pytest_terminal_summary = _real.pytest_terminal_summary
"""

_ISOLATED_TESTS = """
import pytest


@pytest.mark.postgres
def test_marked_runs():
    assert True


@pytest.mark.postgres
@pytest.mark.skip(reason="fixture: a selected postgres-marked test is skipped")
def test_marked_skipped():
    raise AssertionError("should never execute")
"""


def test_postgres_lane_fails_when_a_marked_test_is_skipped(tmp_path):
    """A skipped postgres-marked test (others running) fails the lane closed."""
    (tmp_path / "conftest.py").write_text(_ISOLATED_CONFTEST, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    postgres: pg lane marker\n", encoding="utf-8")
    (tmp_path / "test_marked.py").write_text(_ISOLATED_TESTS, encoding="utf-8")

    # Strip the outer run's coverage/pytest env injections: they point at the
    # parent's config and crash a pytest launched in this isolated tmp rootdir.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV", "PYTEST"))}
    env.update(
        {
            "TESTING": "1",
            "TEST_DB_BACKEND": "postgres",
            "ENVIRONMENT": "test",
            "DJANGO_SECRET_KEY": "postgres-marker-guard-regression",
        }
    )
    # -p no:django: the isolated run needs no Django (plain marked tests), and the
    # outer run exports DJANGO_SETTINGS_MODULE, which pytest-django would try to
    # import from this tmp rootdir and fail.
    result = subprocess.run(
        args=[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:django", "-n0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"guard did not fail on a skipped marked test.\n{combined}"
    assert "skipped" in combined.lower(), f"skip-branch message absent.\n{combined}"


# --- Sharded lane (#2582) ---------------------------------------------------
# CI splits the PostgreSQL lane into pytest-split shards. A shard may run zero
# postgres-marked tests, so it writes evidence and the cross-shard checker owns
# the "at least one ran" rule. A skipped marked test still fails its shard.

_ISOLATED_UNMARKED_TESTS = """
def test_unmarked_runs():
    assert True
"""


def _run_isolated_shard(tmp_path: Path, tests: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    """Run the real guard hooks in an isolated one-shard pytest-split run."""
    tmp_path.mkdir()
    (tmp_path / "conftest.py").write_text(_ISOLATED_CONFTEST, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    postgres: pg lane marker\n", encoding="utf-8")
    (tmp_path / "test_marked.py").write_text(tests, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV", "PYTEST"))}
    env.update({"TESTING": "1", "TEST_DB_BACKEND": "postgres", "ENVIRONMENT": "test"})
    result = subprocess.run(
        args=[
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:django",
            "-n0",
            "--splits",
            "1",
            "--group",
            "1",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    evidence = json.loads((tmp_path / "postgres-lane-evidence.json").read_text(encoding="utf-8"))
    return result, evidence


def test_sharded_lane_defers_zero_ran_rule_but_still_fails_on_skips(tmp_path):
    """A shard with no marked tests passes and records evidence; a skip still fails."""
    passing, evidence = _run_isolated_shard(tmp_path / "unmarked", _ISOLATED_UNMARKED_TESTS)
    assert passing.returncode == 0, passing.stdout + passing.stderr
    assert evidence == {"group": 1, "splits": 1, "ran": [], "skipped": []}

    failing, evidence = _run_isolated_shard(tmp_path / "skipped", _ISOLATED_TESTS)
    assert failing.returncode != 0, failing.stdout + failing.stderr
    assert evidence["ran"] == ["test_marked.py::test_marked_runs", "test_marked.py::test_marked_skipped"]
    assert evidence["skipped"] == ["test_marked.py::test_marked_skipped"]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_postgres_lane_evidence", _SHIFTER_PLATFORM / "scripts/check_postgres_lane_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(group: int, ran: list[str], skipped: list[str] | None = None, splits: int = 2) -> dict[str, object]:
    return {"group": group, "splits": splits, "ran": ran, "skipped": skipped or []}


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ([_record(1, []), _record(2, ["t::a"])], None),
        ([], "no shard evidence found"),
        ([_record(1, []), _record(2, [])], "no postgres-marked tests ran in any shard"),
        ([_record(1, ["t::a"])], "shard groups are incomplete or duplicated: [1]"),
        ([_record(1, ["t::a"]), _record(1, ["t::b"])], "shard groups are incomplete or duplicated: [1, 1]"),
        ([_record(1, ["t::a"]), _record(2, [], splits=3)], "shards disagree on the split count: [2, 3]"),
        ([_record(1, ["t::a"]), _record(2, ["t::b"], ["t::b"])], "1 postgres-marked test(s) skipped: t::b"),
    ],
)
def test_cross_shard_evidence_check(tmp_path, capsys, evidence, expected):
    """The checker fails closed on every broken lane contract and passes a complete lane."""
    paths = []
    for index, record in enumerate(evidence):
        path = tmp_path / f"evidence-{index}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(str(path))
    status = _load_checker().main(paths)
    captured = capsys.readouterr()
    if expected is None:
        assert status == 0, captured.err
        assert "1 postgres-marked tests ran across 2 shards" in captured.out
    else:
        assert status == 1
        assert expected in captured.err
