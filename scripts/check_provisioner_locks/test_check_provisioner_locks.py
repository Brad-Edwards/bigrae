"""Tests for the provisioner lock drift check (#2610)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_provisioner_locks import check_provisioner_locks as locks

_FILES = ("pyproject.toml", "uv.lock", "requirements.lock", "requirements-gcp.lock")


class TestProvisionerLocks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for name in _FILES:
            shutil.copy(locks.PROVISIONER / name, self.tmp / name)

    def test_committed_locks_agree_with_uv_lock(self) -> None:
        self.assertEqual(locks.check(self.tmp), [])

    def test_stale_exports_are_reported(self) -> None:
        for name in ("requirements.lock", "requirements-gcp.lock"):
            path = self.tmp / name
            text = path.read_text(encoding="utf-8")
            pin = next(line for line in text.splitlines() if line.startswith("certifi=="))
            path.write_text(text.replace(pin, "certifi==2000.1.1 \\"), encoding="utf-8")
        errors = locks.check(self.tmp)
        self.assertTrue(any("requirements.lock does not match" in e for e in errors))
        self.assertTrue(any("requirements-gcp.lock pins certifi==2000.1.1" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
