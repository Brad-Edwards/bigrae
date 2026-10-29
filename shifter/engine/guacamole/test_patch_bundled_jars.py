"""Tests for the bundled jar replacement (#2610)."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import patch_bundled_jars as patch


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return out.getvalue()


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class TestPatchBundledJars(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "guava-33.7.2-jre.jar").write_bytes(b"fixed guava")
        (self.tmp / "jackson-core-2.21.8.jar").write_bytes(b"fixed jackson")
        self.replace = [
            "--replace",
            f"guava-[0-9.]+-jre\\.jar={self.tmp / 'guava-33.7.2-jre.jar'}",
            "--replace",
            f"jackson-core-[0-9.]+\\.jar={self.tmp / 'jackson-core-2.21.8.jar'}",
        ]
        nested = _zip({"guava-32.1.3-jre.jar": b"old guava", "base.properties": b"kept"})
        self.webapp = self.tmp / "guacamole.war"
        self.webapp.write_bytes(
            _zip(
                {
                    "WEB-INF/web.xml": b"<web-app/>",
                    "WEB-INF/lib/guava-32.1.3-jre.jar": b"old guava",
                    "WEB-INF/lib/jackson-core-2.19.0.jar": b"old jackson",
                    "WEB-INF/lib/other-1.0.jar": _zip({"Other.class": b"other"}),
                }
            )
        )
        self.extension = self.tmp / "guacamole-auth-jdbc-postgresql.jar"
        self.extension.write_bytes(_zip({"guacamole-auth-jdbc-base-1.6.0.jar": nested, "guac-manifest.json": b"{}"}))

    def _run(self, *archives: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = patch.main([*self.replace, *map(str, archives)])
        return code, stderr.getvalue()

    def test_replaces_direct_and_nested_jars_and_keeps_other_entries(self) -> None:
        self.assertEqual(self._run(self.webapp, self.extension), (0, ""))

        webapp = _entries(self.webapp.read_bytes())
        self.assertEqual(
            sorted(webapp),
            [
                "WEB-INF/lib/guava-33.7.2-jre.jar",
                "WEB-INF/lib/jackson-core-2.21.8.jar",
                "WEB-INF/lib/other-1.0.jar",
                "WEB-INF/web.xml",
            ],
        )
        self.assertEqual(webapp["WEB-INF/lib/guava-33.7.2-jre.jar"], b"fixed guava")
        self.assertEqual(webapp["WEB-INF/lib/jackson-core-2.21.8.jar"], b"fixed jackson")
        self.assertEqual(_entries(webapp["WEB-INF/lib/other-1.0.jar"]), {"Other.class": b"other"})
        with zipfile.ZipFile(self.webapp) as archive:
            self.assertEqual(archive.getinfo("WEB-INF/lib/guava-33.7.2-jre.jar").compress_type, zipfile.ZIP_STORED)

        extension = _entries(self.extension.read_bytes())
        self.assertEqual(extension["guac-manifest.json"], b"{}")
        base = _entries(extension["guacamole-auth-jdbc-base-1.6.0.jar"])
        self.assertEqual(base, {"guava-33.7.2-jre.jar": b"fixed guava", "base.properties": b"kept"})

    def test_layout_changes_fail_and_write_nothing(self) -> None:
        empty = self.tmp / "guacamole-auth-json.jar"
        empty.write_bytes(_zip({"guac-manifest.json": b"{}"}))
        before = {path: path.read_bytes() for path in (self.webapp, self.extension, empty)}

        code, stderr = self._run(self.webapp, empty)
        self.assertEqual(code, 1)
        self.assertIn("guacamole-auth-json.jar: no bundled jar matched", stderr)

        # The extension has guava but no jackson-core, so jackson-core matches no archive.
        code, stderr = self._run(self.extension)
        self.assertEqual(code, 1)
        self.assertIn("no archive bundles a jar that matches: jackson-core-", stderr)

        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_replace_argument_needs_pattern_and_path(self) -> None:
        for value in ("guava.jar", "=/patch/guava.jar", "guava-.*="):
            with self.subTest(value=value), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                patch.main(["--replace", value, str(self.webapp)])


if __name__ == "__main__":
    unittest.main()
