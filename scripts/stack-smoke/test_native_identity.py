"""Check local fixture isolation before any Docker effects or credential reads."""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DIRECTORY = Path(__file__).parent
spec = importlib.util.spec_from_file_location("publish_openfga_model", DIRECTORY / "publish_openfga_model.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class PublicationBoundaryTests(unittest.TestCase):
    def test_rejects_other_destinations_before_reading_credentials(self):
        for url in (
            "http://localhost:8080",
            "https://example.invalid:8080",
            "https://localhost:8080@example.invalid",
            "https://localhost:8080/path",
            "https://localhost:8080?query=value",
            "https://operator@localhost:8080",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "localhost HTTPS"):
                publisher.publish(url, "/missing-ca", "/missing-token", "/missing-model")


class CollisionTests(unittest.TestCase):
    def test_existing_container_or_network_is_never_removed(self):
        for collision in ("container", "network"):
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                fixture = Path(directory)
                docker = fixture / "docker"
                docker.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "with open(os.environ['SMOKE_TEST_CALLS'], 'a') as calls:\n"
                    "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "args = sys.argv[1:]\n"
                    "if args[:2] == [os.environ['SMOKE_TEST_COLLISION'], 'inspect']:\n"
                    "    sys.exit(0)\n"
                    "if args[:2] in (['container', 'inspect'], ['network', 'inspect']):\n"
                    "    sys.exit(1)\n"
                    "if args[:2] == ['ps', '-aq']:\n"
                    "    sys.exit(0)\n"
                    "sys.exit(99)\n",
                    encoding="utf-8",
                )
                docker.chmod(0o700)
                calls = fixture / "calls.jsonl"
                env = {
                    **os.environ,
                    "PATH": f"{fixture}:{os.environ['PATH']}",
                    "SMOKE_TEST_CALLS": str(calls),
                    "SMOKE_TEST_COLLISION": collision,
                    "SMOKE_BUILD": "0",
                }
                # The command is the repository harness; only its Docker
                # executable is a local recorder. No shell input is evaluated.
                result = subprocess.run(  # noqa: S603 — fixed repository harness
                    [shutil.which("bash"), str(DIRECTORY / "stack_smoke.sh")],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("name already exists", result.stderr)
                recorded = calls.read_text(encoding="utf-8")
                self.assertNotIn('"rm"', recorded)
                self.assertNotIn('"run"', recorded)
                self.assertNotIn('"create"', recorded)


if __name__ == "__main__":
    unittest.main()
